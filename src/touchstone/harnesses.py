"""Resolve one explicit project Harness without leaking machine-local paths."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import tomli_w

from touchstone.config import Config, ConfigError
from touchstone.execution.local import LocalExecutor


@dataclass(frozen=True, slots=True)
class HarnessContext:
    mode: str
    source: str
    entrypoint: Path
    revision: str
    context_root: Path
    evidence: tuple[str, ...]


class HarnessResolutionError(RuntimeError):
    """A stable blocked result produced before any model session starts."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def registry_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "touchstone" / "harnesses.toml"


def load_registry(path: Path | None = None) -> dict[str, Path]:
    """Load only canonical Harness identities and local checkout paths."""

    chosen = (path or registry_path()).expanduser()
    try:
        raw = tomllib.loads(chosen.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{chosen} is not valid TOML: {exc}") from None
    if set(raw) - {"harnesses"}:
        raise ConfigError("local Harness registry accepts only [harnesses] entries")
    entries = raw.get("harnesses", {})
    if not isinstance(entries, dict):
        raise ConfigError("harnesses must be a table")
    result: dict[str, Path] = {}
    for source, value in entries.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
            raise ConfigError(f"harnesses.{source} must use a canonical owner/repository identity")
        if not isinstance(value, dict) or set(value) != {"path"}:
            raise ConfigError(f"harnesses.{source} must contain only path")
        local = value["path"]
        if not isinstance(local, str) or not Path(local).expanduser().is_absolute():
            raise ConfigError(f"harnesses.{source}.path must be an absolute local path")
        result[source] = Path(local).expanduser().resolve()
    return result


def write_registry(entries: dict[str, Path], path: Path | None = None) -> Path:
    """Atomically replace only the machine-local Harness registry."""

    chosen = (path or registry_path()).expanduser()
    chosen.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "harnesses": {
            source: {"path": str(local.expanduser().resolve())}
            for source, local in sorted(entries.items())
        }
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix=".harnesses-", dir=chosen.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(tomli_w.dumps(data))
        temporary.chmod(0o600)
        temporary.replace(chosen)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return chosen


def register_harness(source: str, checkout: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
        raise ConfigError("Harness source must use a canonical owner/repository identity")
    local = checkout.expanduser().resolve()
    if not local.is_dir():
        raise ConfigError(f"Harness checkout is not a directory: {checkout}")
    entries = load_registry()
    entries[source] = local
    return write_registry(entries)


def unregister_harness(source: str) -> Path:
    entries = load_registry()
    entries.pop(source, None)
    return write_registry(entries)


def resolve_harness(
    config: Config,
    *,
    registry: dict[str, Path] | None = None,
    snapshot_root: Path | None = None,
) -> HarnessContext:
    """Resolve the declared Harness from the same boundary used by a run."""

    declaration = config.harness
    if declaration is None:
        raise HarnessResolutionError(
            "harness-missing",
            "configuration has no explicit [harness] declaration",
        )
    if declaration.mode == "embedded":
        return _resolve_embedded(config)
    return _resolve_external(
        config,
        registry=load_registry() if registry is None else registry,
        snapshot_root=snapshot_root,
    )


def _safe_entrypoint(root: Path, reference: str) -> Path:
    candidate = (root / reference).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise HarnessResolutionError(
            "harness-identity-mismatch",
            "Harness entrypoint escapes its declared root",
        )
    if not candidate.is_file():
        raise HarnessResolutionError(
            "harness-entrypoint-missing",
            f"Harness entrypoint does not exist: {reference}",
        )
    return candidate


_GENERATED = re.compile(r"Generated by [^;]+;[^\n>]*\bsource=([^\s>]+)")


def _resolve_embedded(config: Config) -> HarnessContext:
    assert config.harness is not None
    root = config.repo_path.resolve()
    entrypoint = _safe_entrypoint(root, config.harness.entrypoint)
    text = entrypoint.read_text(encoding="utf-8")
    generated = _GENERATED.search(text)
    marker_present = "Generated by " in text[:1000]
    if marker_present and generated is None:
        raise HarnessResolutionError(
            "harness-provenance-invalid",
            "generated Harness entrypoint does not contain parseable source provenance",
        )
    if generated:
        revision = generated.group(1)
        evidence = (f"generated-source:{revision}",)
    else:
        revision = _git_revision(root) or "repository"
        evidence = ("repository-entrypoint",)
    return HarnessContext(
        mode="embedded",
        source="repository",
        entrypoint=entrypoint,
        revision=revision,
        context_root=root,
        evidence=evidence,
    )


def _resolve_external(
    config: Config,
    *,
    registry: dict[str, Path],
    snapshot_root: Path | None,
) -> HarnessContext:
    assert config.harness is not None
    declaration = config.harness
    checkout = registry.get(declaration.source)
    if checkout is None or not checkout.is_dir():
        raise HarnessResolutionError(
            "checkout-not-installed",
            f"external Harness {declaration.source!r} is not registered on this machine",
        )
    target_rules = config.repo_path / "AGENTS.md"
    try:
        target_text = target_rules.read_text(encoding="utf-8")
    except FileNotFoundError:
        target_text = ""
    if declaration.source not in target_text:
        raise HarnessResolutionError(
            "harness-identity-mismatch",
            "target repository instructions do not identify the configured external Harness",
        )
    executor = LocalExecutor()
    remote = declaration.ref.partition("/")[0]
    if not remote or not declaration.ref.partition("/")[2]:
        raise HarnessResolutionError(
            "harness-identity-mismatch",
            "external Harness ref must name a remote and revision, for example origin/main",
        )
    remote_result = executor.run(
        ["git", "remote", "get-url", remote], cwd=str(checkout), timeout=30
    )
    if (
        not remote_result.ok
        or _canonical_source(remote_result.stdout.strip()) != declaration.source
    ):
        raise HarnessResolutionError(
            "harness-identity-mismatch",
            f"registered checkout remote does not match {declaration.source}",
        )
    fetched = executor.run(["git", "fetch", "--quiet", remote], cwd=str(checkout), timeout=300)
    if not fetched.ok:
        raise HarnessResolutionError(
            "harness-identity-mismatch",
            "external Harness remote could not be fetched",
        )
    resolved = executor.run(
        ["git", "rev-parse", "--verify", f"{declaration.ref}^{{commit}}"],
        cwd=str(checkout),
        timeout=30,
    )
    if not resolved.ok:
        raise HarnessResolutionError(
            "harness-identity-mismatch",
            f"external Harness ref could not be resolved: {declaration.ref}",
        )
    revision = resolved.stdout.strip()
    parent = (snapshot_root or (config.state_dir / "harness-context")).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix="snapshot-", dir=parent))
    try:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", revision],
            cwd=checkout,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        archive = None
    if archive is None or archive.returncode != 0:
        shutil.rmtree(snapshot)
        raise HarnessResolutionError(
            "harness-entrypoint-missing",
            "external Harness revision could not be materialized",
        )
    try:
        with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as bundle:
            bundle.extractall(snapshot, filter="data")
        entrypoint = _safe_entrypoint(snapshot, declaration.entrypoint)
    except (OSError, tarfile.TarError, HarnessResolutionError):
        shutil.rmtree(snapshot)
        raise HarnessResolutionError(
            "harness-entrypoint-missing",
            f"Harness entrypoint does not exist at {declaration.ref}: {declaration.entrypoint}",
        ) from None
    _make_read_only(snapshot)
    return HarnessContext(
        mode="external",
        source=declaration.source,
        entrypoint=entrypoint,
        revision=revision,
        context_root=snapshot,
        evidence=(f"remote:{remote}", f"ref:{declaration.ref}"),
    )


def _canonical_source(remote_url: str) -> str:
    value = remote_url.removesuffix(".git").rstrip("/")
    if "://" in value:
        parsed = urlsplit(value)
        path = parsed.path.strip("/")
    elif ":" in value and not value.startswith("/"):
        path = value.split(":", 1)[1].strip("/")
    else:
        path = Path(value).as_posix().strip("/")
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else path


def _git_revision(root: Path) -> str:
    result = LocalExecutor().run(["git", "rev-parse", "HEAD"], cwd=str(root), timeout=30)
    return result.stdout.strip() if result.ok else ""


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


__all__ = [
    "HarnessContext",
    "HarnessResolutionError",
    "load_registry",
    "register_harness",
    "registry_path",
    "resolve_harness",
    "unregister_harness",
    "write_registry",
]
