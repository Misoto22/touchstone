"""Deterministic Profile materialization and generated-file refresh."""

from __future__ import annotations

import difflib
import hashlib
import json
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import tomli_w

from touchstone.config import Config, ConfigError, load
from touchstone.profiles.catalog import load_catalog
from touchstone.profiles.detect import detect_profiles
from touchstone.profiles.model import Evidence, ProfileCatalog, ProfileMatch, TargetCandidate
from touchstone.profiles.targets import TargetDiscovery, discover_targets

_MANAGER_LOCKS = (
    ("npm", "node", ("package-lock.json", "npm-shrinkwrap.json")),
    ("pnpm", "node", ("pnpm-lock.yaml",)),
    ("yarn", "node", ("yarn.lock",)),
    ("bun", "node", ("bun.lock", "bun.lockb")),
    ("uv", "python", ("uv.lock",)),
    ("poetry", "python", ("poetry.lock",)),
    ("pdm", "python", ("pdm.lock",)),
)


@dataclass(frozen=True, slots=True)
class MaterializedConfig:
    data: dict[str, Any]
    text: str
    source_digest: str
    discovery: TargetDiscovery
    matches: dict[str, tuple[ProfileMatch, ...]]


@dataclass(frozen=True, slots=True)
class ProfileDiff:
    changed: bool
    diff: str
    current_text: str
    expected_text: str
    materialized: MaterializedConfig


@dataclass(frozen=True, slots=True)
class RefreshReport:
    path: Path
    changed: bool
    written: bool
    diff: str


def detect_repository(
    root: Path, *, explicit_profiles: tuple[str, ...] = ()
) -> tuple[TargetDiscovery, dict[str, tuple[ProfileMatch, ...]], ProfileCatalog]:
    repository = root.expanduser().resolve()
    catalog = load_catalog(repository / ".touchstone/profiles")
    for name in explicit_profiles:
        catalog.get(name)
    discovery = discover_targets(repository)
    matches: dict[str, tuple[ProfileMatch, ...]] = {}
    for target in discovery.targets:
        detected = list(
            detect_profiles(
                repository,
                TargetCandidate(id=target.id, path=target.path),
            )
        )
        present = {match.profile for match in detected}
        for name in explicit_profiles:
            if name not in present:
                detected.append(
                    ProfileMatch(
                        name,
                        "confirmed",
                        (
                            Evidence(
                                "explicit",
                                target.path.as_posix(),
                                "selected by project owner",
                            ),
                        ),
                    )
                )
        order = {name: index for index, name in enumerate(catalog.profiles)}
        matches[target.id] = tuple(
            sorted(detected, key=lambda match: order.get(match.profile, len(order)))
        )
    return discovery, matches, catalog


def materialize(
    discovery: TargetDiscovery,
    matches: dict[str, tuple[ProfileMatch, ...]],
    catalog: ProfileCatalog,
    *,
    package_managers: tuple[str, ...] = (),
) -> MaterializedConfig:
    used_profiles: list[str] = []
    targets: dict[str, Any] = {}
    protected = list(catalog.get("generic").protected_paths)
    source_paths: list[str] = []
    context: list[str] = []

    for target in discovery.targets:
        target_matches = matches.get(target.id, ())
        confirmed = [match.profile for match in target_matches if match.verdict == "confirmed"]
        for profile in confirmed:
            if profile not in used_profiles:
                used_profiles.append(profile)
        evidence = []
        validations = []
        target_protected: list[str] = []
        target_sources: list[str] = []
        audit_context: list[str] = []
        for match in target_matches:
            definition = catalog.get(match.profile)
            for item in match.evidence:
                record: dict[str, Any] = {
                    "profile": match.profile,
                    "verdict": match.verdict,
                    "kind": item.kind,
                    "path": item.path,
                    "detail": item.detail,
                }
                if match.detected_version is not None:
                    record["detected_version"] = match.detected_version
                if match.warning:
                    record["warning"] = match.warning
                evidence.append(record)
            if match.verdict != "confirmed":
                continue
            _extend_unique(target_protected, definition.protected_paths)
            _extend_unique(target_sources, definition.source_paths)
            if definition.audit_context and definition.audit_context not in audit_context:
                audit_context.append(definition.audit_context)
            for candidate in definition.validation:
                record = {
                    "argv": list(candidate.argv),
                    "timeout_seconds": candidate.timeout_seconds,
                    "capability": candidate.capability,
                    "enabled": candidate.enabled,
                }
                if record not in validations:
                    validations.append(record)
        scoped_protected = [_scoped(target.path, value) for value in target_protected]
        scoped_sources = [_scoped(target.path, value) for value in target_sources]
        _extend_unique(protected, scoped_protected)
        _extend_unique(source_paths, scoped_sources)
        label = ", ".join(confirmed) or "generic"
        context.append(f"{target.id} at {target.path.as_posix()} ({label})")
        targets[target.id] = {
            "path": target.path.as_posix(),
            "profiles": confirmed,
            "dependencies": list(target.dependencies),
            "audit_context": audit_context,
            "protected_paths": target_protected,
            "source_paths": target_sources,
            "evidence": evidence,
            "validation": validations,
        }

    profile_versions = {
        name: catalog.get(name).version
        for name in catalog.profiles
        if name in used_profiles or name == "generic"
    }
    data: dict[str, Any] = {
        "metadata": {
            "package_version": _package_version(),
            "package_managers": list(package_managers),
            "profile_versions": profile_versions,
        },
        "target": targets,
        "loop": {
            "code": {
                "protected_paths": protected,
                "require_change_under": source_paths,
                "context": {
                    "project": "; ".join(context) or "this repository",
                    "ledger": (
                        "No project findings ledger is configured; treat the queue as empty."
                    ),
                    "protected": "the generated and project-owned protected paths",
                    "rules_clause": "",
                },
            }
        },
    }
    digest = _digest(data)
    data["metadata"]["source_digest"] = digest
    text = (
        "# Generated by `touchstone profile refresh`; edit touchstone.toml instead.\n"
        "# Run `touchstone profile diff` to inspect regeneration.\n\n" + tomli_w.dumps(data)
    )
    return MaterializedConfig(data, text, digest, discovery, matches)


def profile_diff(config: Config) -> ProfileDiff:
    generated_path = config.source.generated_path
    if generated_path is None:
        raise ConfigError("Profile refresh requires a version 2 configuration")
    discovery, matches, catalog = detect_repository(config.repo_path)
    order = {name: index for index, name in enumerate(catalog.profiles)}
    for target in discovery.targets:
        configured = config.targets.get(target.id)
        if configured is None:
            continue
        detected = list(matches[target.id])
        present = {match.profile for match in detected}
        for name in configured.profiles:
            if name in present:
                continue
            catalog.get(name)
            detected.append(
                ProfileMatch(
                    name,
                    "confirmed",
                    (
                        Evidence(
                            "explicit",
                            target.path.as_posix(),
                            "selected by project owner",
                        ),
                    ),
                )
            )
        matches[target.id] = tuple(
            sorted(detected, key=lambda match: order.get(match.profile, len(order)))
        )
    managers = (
        config.generated_metadata.package_managers if config.generated_metadata is not None else ()
    )
    generated = materialize(discovery, matches, catalog, package_managers=managers)
    try:
        current = generated_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    changed = current != generated.text
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            generated.text.splitlines(keepends=True),
            fromfile=str(generated_path),
            tofile=f"{generated_path} (regenerated)",
        )
    )
    return ProfileDiff(changed, diff, current, generated.text, generated)


def refresh_profiles(config_path: Path, *, write: bool) -> RefreshReport:
    config = load(config_path)
    report = profile_diff(config)
    path = config.source.generated_path
    if path is None:
        raise ConfigError("Profile refresh requires a version 2 configuration")
    written = False
    if write and report.changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.refreshing")
        try:
            temporary.write_text(report.expected_text, encoding="utf-8")
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        written = True
    return RefreshReport(path, report.changed, written, report.diff)


def detect_package_managers(root: Path) -> tuple[str, ...]:
    repository = root.expanduser().resolve()
    found: list[str] = []
    for manager, _ecosystem, files in _MANAGER_LOCKS:
        if any((repository / name).is_file() for name in files):
            found.append(manager)
    package = _read_json(repository / "package.json")
    declared = package.get("packageManager") if package is not None else None
    if isinstance(declared, str):
        manager = declared.split("@", 1)[0]
        if manager in {item[0] for item in _MANAGER_LOCKS} and manager not in found:
            found.append(manager)
    pyproject = _read_toml(repository / "pyproject.toml")
    tool = pyproject.get("tool", {}) if pyproject is not None else {}
    if isinstance(tool, dict):
        for manager in ("uv", "poetry", "pdm"):
            if manager in tool and manager not in found:
                found.append(manager)
    order = {manager: index for index, (manager, _, _) in enumerate(_MANAGER_LOCKS)}
    return tuple(sorted(found, key=lambda manager: order[manager]))


def ambiguous_package_managers(managers: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    ecosystems = {manager: ecosystem for manager, ecosystem, _ in _MANAGER_LOCKS}
    groups = []
    for ecosystem in ("node", "python"):
        group = tuple(manager for manager in managers if ecosystems[manager] == ecosystem)
        if len(group) > 1:
            groups.append(group)
    return tuple(groups)


def select_package_managers(root: Path, explicit: str | None = None) -> tuple[str, ...]:
    found = detect_package_managers(root)
    ambiguous = ambiguous_package_managers(found)
    if not ambiguous:
        if explicit is not None and explicit not in found:
            raise ConfigError(f"package manager {explicit!r} has no repository evidence")
        return found
    if explicit is None:
        choices = ", ".join("/".join(group) for group in ambiguous)
        raise ConfigError(
            f"ambiguous package manager evidence ({choices}); pass --package-manager NAME"
        )
    if explicit not in found:
        raise ConfigError(f"package manager {explicit!r} has no repository evidence")
    ambiguous_names = {name for group in ambiguous for name in group}
    return tuple(name for name in found if name not in ambiguous_names or name == explicit)


def _scoped(target: Path, value: str) -> str:
    if target == Path("."):
        return value
    return (target / value).as_posix()


def _extend_unique(values: list[str], additions: tuple[str, ...] | list[str]) -> None:
    for value in additions:
        if value not in values:
            values.append(value)


def _digest(data: dict[str, Any]) -> str:
    normalized = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(normalized.encode()).hexdigest()}"


def _package_version() -> str:
    try:
        return version("touchstone-agent")
    except PackageNotFoundError:
        return "0+unknown"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


__all__ = [
    "MaterializedConfig",
    "ProfileDiff",
    "RefreshReport",
    "ambiguous_package_managers",
    "detect_package_managers",
    "detect_repository",
    "materialize",
    "profile_diff",
    "refresh_profiles",
    "select_package_managers",
]
