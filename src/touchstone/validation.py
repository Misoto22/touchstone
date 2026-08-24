"""Structured, secret-scrubbed Validation Gates."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from touchstone.config import Config
from touchstone.execution import Executor
from touchstone.profiles.targets import (
    ProjectTarget,
    TargetDiscovery,
    changed_target_scope,
)

ValidationOutcome = Literal["completed", "blocked"]
_YARN_MODERN_MARKER = ".yarnrc.yml"
_SECRET_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
)
_ALLOWED_ENVIRONMENT = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "UV_CACHE_DIR",
    "XDG_CACHE_HOME",
    "npm_config_cache",
}
_SHELLS = {"bash", "cmd", "dash", "pwsh", "sh", "zsh"}


@dataclass(frozen=True, slots=True)
class ValidationCommand:
    target: str
    argv: tuple[str, ...]
    cwd: Path = Path(".")
    timeout_seconds: int = 300
    capability: str = "source-read"
    enabled: bool = False
    preparation: str = "none"
    shell: bool = False
    risk_acknowledged: bool = False
    allow_scripts: bool = False
    allow_build_hooks: bool = False
    extra_env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or any(not isinstance(item, str) or not item for item in self.argv)
        ):
            raise ValueError("Validation Gate argv must be a non-empty tuple of strings")
        if self.timeout_seconds <= 0:
            raise ValueError("Validation Gate timeout must be positive")
        if self.cwd.is_absolute() or ".." in self.cwd.parts:
            raise ValueError("Target working directory must stay inside the Target")
        if self.shell and (Path(self.argv[0]).name not in _SHELLS or not self.risk_acknowledged):
            raise ValueError(
                "shell Validation Gates require an explicit shell executable "
                "and risk acknowledgement"
            )
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            or any(marker in name.upper() for marker in _SECRET_MARKERS)
            for name, value in self.extra_env
        ):
            raise ValueError("Validation Gate extra_env must hold non-secret string pairs")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    target: str
    argv: tuple[str, ...]
    code: int | None
    stdout: str
    stderr: str
    reason: str
    timed_out: bool = False
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.reason in {"passed", "disabled"}


@dataclass(frozen=True, slots=True)
class ValidationReport:
    outcome: ValidationOutcome
    results: tuple[ValidationResult, ...]

    @property
    def blocked(self) -> bool:
        return self.outcome == "blocked"


@dataclass(frozen=True, slots=True)
class PreparationReport:
    outcome: ValidationOutcome
    results: tuple[ValidationResult, ...]


def run_gate(
    repository: Path,
    target_path: Path,
    command: ValidationCommand,
    executor: Executor,
    *,
    env: Mapping[str, str] | None = None,
) -> ValidationResult:
    if not command.enabled:
        return ValidationResult(command.target, command.argv, None, "", "", "disabled")
    root = repository.expanduser().resolve()
    target = (root / target_path).resolve()
    cwd = (target / command.cwd).resolve()
    if not target.is_relative_to(root) or not cwd.is_relative_to(target):
        raise ValueError("Target working directory must stay inside the Target")
    if not executor.exists(str(cwd)):
        return ValidationResult(
            command.target,
            command.argv,
            None,
            "",
            "Target working directory does not exist",
            "target-missing",
        )
    before = _status(executor, root)
    if before is None:
        return ValidationResult(
            command.target,
            command.argv,
            None,
            "",
            "could not inspect tracked files",
            "status-unavailable",
        )
    started = time.monotonic()
    try:
        result = executor.run(
            list(command.argv),
            cwd=str(cwd),
            timeout=command.timeout_seconds,
            env=_sanitized_environment(env or os.environ, extra=command.extra_env),
        )
    except OSError as exc:
        return ValidationResult(
            command.target,
            command.argv,
            None,
            "",
            str(exc),
            "command-unavailable",
            duration_seconds=time.monotonic() - started,
        )
    duration = time.monotonic() - started
    after = _status(executor, root)
    if after is None:
        reason = "status-unavailable"
    elif after != before:
        reason = "tracked-files-changed"
    elif result.timed_out:
        reason = "timeout"
    elif not result.ok:
        reason = "command-failed"
    else:
        reason = "passed"
    return ValidationResult(
        command.target,
        command.argv,
        result.code,
        result.stdout,
        result.stderr,
        reason,
        result.timed_out,
        duration,
    )


def validate_commands(
    repository: Path,
    target_path: Path,
    commands: tuple[ValidationCommand, ...],
    executor: Executor,
    *,
    env: Mapping[str, str] | None = None,
) -> ValidationReport:
    results = tuple(
        run_gate(repository, target_path, command, executor, env=env) for command in commands
    )
    outcome: ValidationOutcome = (
        "blocked" if any(not result.ok for result in results) else "completed"
    )
    return ValidationReport(outcome, results)


def validate(
    config: Config,
    targets: tuple[str, ...],
    executor: Executor,
    *,
    repository: Path | None = None,
) -> ValidationReport:
    root = (repository or config.repo_path).expanduser().resolve()
    selected = targets or tuple(config.targets)
    results: list[ValidationResult] = []
    for target_id in selected:
        target = config.targets.get(target_id)
        if target is None:
            results.append(
                ValidationResult(
                    target_id,
                    (),
                    None,
                    "",
                    "configured Target does not exist",
                    "target-missing",
                )
            )
            continue
        commands = tuple(
            ValidationCommand(
                target=target_id,
                argv=gate.argv,
                cwd=gate.cwd,
                timeout_seconds=gate.timeout_seconds,
                capability=gate.capability,
                enabled=gate.enabled,
                preparation=gate.preparation,
                shell=gate.shell,
                risk_acknowledged=gate.risk_acknowledged,
                allow_scripts=gate.allow_scripts,
                allow_build_hooks=gate.allow_build_hooks,
            )
            for gate in target.validation
        )
        report = validate_commands(root, target.path, commands, executor)
        results.extend(report.results)
    outcome: ValidationOutcome = (
        "blocked" if any(not result.ok for result in results) else "completed"
    )
    return ValidationReport(outcome, tuple(results))


_MANAGER_LOCKFILES = {
    "npm": ("package-lock.json", "npm-shrinkwrap.json"),
    "pnpm": ("pnpm-lock.yaml",),
    "yarn": ("yarn.lock",),
    "bun": ("bun.lock", "bun.lockb"),
    "uv": ("uv.lock",),
    "poetry": ("poetry.lock",),
    "pdm": ("pdm.lock",),
}
_MANAGER_DIRECTORIES = {
    "npm": ("node_modules",),
    "pnpm": ("node_modules",),
    "yarn": ("node_modules",),
    "bun": ("node_modules",),
    "uv": (".venv",),
    "poetry": (".venv",),
    "pdm": (".venv", "__pypackages__"),
}


def preparation_lockfiles(config: Config, targets: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Name every repository-relative lockfile a locked preparation depends on."""

    return _manager_paths(config, targets, _MANAGER_LOCKFILES)


def preparation_directories(config: Config, targets: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Name the repository-relative directories a locked preparation populates."""

    return _manager_paths(config, targets, _MANAGER_DIRECTORIES)


def _manager_paths(
    config: Config,
    targets: tuple[str, ...],
    names_by_manager: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    selected = targets or tuple(config.targets)
    found: list[str] = []
    for target_id in selected:
        target = config.targets.get(target_id)
        if target is None:
            continue
        for manager in target.package_managers:
            for name in names_by_manager.get(manager, ()):
                value = (target.path / name).as_posix()
                if value not in found:
                    found.append(value)
    return tuple(sorted(found))


def affected_validation_targets(
    config: Config,
    targets: tuple[str, ...],
    executor: Executor,
    *,
    repository: Path,
) -> tuple[str, ...]:
    """Choose which of a Loop's Targets one worktree change actually needs.

    Direct owners and their reverse dependents are validated. A change that
    cannot be attributed to a narrow Target — a repository-root or otherwise
    shared edit — widens back to every Target the Loop configures.
    """

    configured = tuple(targets) or tuple(config.targets)
    payload = _status(executor, repository.expanduser().resolve())
    if payload is None:
        return configured
    changed = _changed_paths(payload)
    if not changed:
        return ()
    scope, conservative = changed_target_scope(changed, _configured_discovery(config))
    if conservative:
        return configured
    return tuple(target for target in configured if target in scope)


def validate_affected(
    config: Config,
    targets: tuple[str, ...],
    executor: Executor,
    *,
    repository: Path | None = None,
) -> ValidationReport:
    """Run Validation Gates for only the Targets a change can reach."""

    root = (repository or config.repo_path).expanduser().resolve()
    selected = affected_validation_targets(config, targets, executor, repository=root)
    if not selected:
        return ValidationReport("completed", ())
    return validate(config, selected, executor, repository=root)


def _configured_discovery(config: Config) -> TargetDiscovery:
    """Describe the reviewed Target graph without re-walking the repository."""

    targets = tuple(
        ProjectTarget(
            id=target.id,
            path=target.path,
            dependencies=tuple(target.dependencies),
        )
        for target in config.targets.values()
    )
    return TargetDiscovery(targets=targets, candidates=(), excluded=(), warnings=())


def _changed_paths(payload: str) -> tuple[str, ...]:
    """Read repository-relative paths from `git status --porcelain=v1 -z`."""

    entries = payload.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4 or entry[2] != " ":
            continue
        status = entry[:2]
        paths.append(entry[3:])
        if ("R" in status or "C" in status) and index < len(entries):
            original = entries[index]
            index += 1
            if original:
                paths.append(original)
    return tuple(dict.fromkeys(paths))


def prepare(
    config: Config,
    targets: tuple[str, ...],
    executor: Executor,
    *,
    repository: Path | None = None,
) -> PreparationReport:
    root = (repository or config.repo_path).expanduser().resolve()
    selected = targets or tuple(config.targets)
    results: list[ValidationResult] = []
    for target_id in selected:
        target = config.targets.get(target_id)
        if target is None:
            continue
        requirements = [
            gate
            for gate in target.validation
            if gate.enabled and gate.preparation == "locked-install"
        ]
        if not requirements:
            continue
        allow_scripts = any(gate.allow_scripts for gate in requirements)
        allow_build_hooks = any(gate.allow_build_hooks for gate in requirements)
        managers = target.package_managers or ("",)
        for manager in managers:
            try:
                command = _preparation_command(
                    manager,
                    target_id,
                    root=root,
                    target_path=target.path,
                    allow_scripts=allow_scripts,
                    allow_build_hooks=allow_build_hooks,
                )
            except ValueError as exc:
                # A preparation policy that cannot be satisfied is a structured,
                # fail-closed result rather than a crash inside a hosted stage.
                results.append(
                    ValidationResult(
                        target_id,
                        getattr(exc, "argv", ()) or (manager or "unknown",),
                        None,
                        "",
                        str(exc),
                        getattr(exc, "reason", "policy-unsupported"),
                    )
                )
                continue
            results.append(run_gate(root, target.path, command, executor))
    outcome: ValidationOutcome = (
        "blocked" if any(not result.ok for result in results) else "completed"
    )
    return PreparationReport(outcome, tuple(results))


class PreparationPolicyError(ValueError):
    """A locked preparation policy that this package manager cannot satisfy."""

    def __init__(self, message: str, *, reason: str, argv: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.reason = reason
        self.argv = argv


def _preparation_command(
    manager: str,
    target: str,
    *,
    root: Path,
    target_path: Path,
    allow_scripts: bool,
    allow_build_hooks: bool,
) -> ValidationCommand:
    """Build one hook-free locked install for a confirmed package manager."""

    extra_env: tuple[tuple[str, str], ...] = ()
    if manager == "npm":
        argv = ("npm", "ci") + (() if allow_scripts else ("--ignore-scripts",))
    elif manager == "pnpm":
        argv = ("pnpm", "install", "--frozen-lockfile") + (
            () if allow_scripts else ("--ignore-scripts",)
        )
    elif manager == "bun":
        argv = ("bun", "install", "--frozen-lockfile") + (
            () if allow_scripts else ("--ignore-scripts",)
        )
    elif manager == "yarn":
        if _yarn_is_modern(root, target_path):
            # Berry replaced --frozen-lockfile with --immutable, and skips
            # package build steps through the install mode rather than a flag.
            argv = ("yarn", "install", "--immutable") + (
                () if allow_scripts else ("--mode=skip-build",)
            )
        else:
            argv = ("yarn", "install", "--frozen-lockfile") + (
                () if allow_scripts else ("--ignore-scripts",)
            )
    elif manager == "uv":
        argv = ("uv", "sync", "--frozen") + (
            () if allow_build_hooks else ("--no-install-workspace", "--no-build")
        )
    elif manager == "pdm":
        argv = ("pdm", "sync", "--frozen-lockfile") + (() if allow_build_hooks else ("--no-self",))
        if not allow_build_hooks:
            extra_env = (("PDM_ONLY_BINARY", ":all:"),)
    elif manager == "poetry":
        if not allow_build_hooks:
            # Poetry has no supported switch that guarantees every dependency is
            # installed without running its build hooks.
            raise PreparationPolicyError(
                "hook-free locked preparation is not supported for poetry; "
                "set allow_build_hooks = true on the Validation Gate to accept "
                "project build hooks, or select a package manager that can "
                "install from binaries only",
                reason="policy-unsupported",
                argv=("poetry", "install"),
            )
        argv = ("poetry", "install")
    else:
        raise PreparationPolicyError(
            "locked preparation requires a confirmed package manager",
            reason="policy-unsupported",
        )
    return ValidationCommand(
        target=target,
        argv=argv,
        timeout_seconds=1200,
        capability="locked-install",
        enabled=True,
        extra_env=extra_env,
    )


def _yarn_is_modern(root: Path, target_path: Path) -> bool:
    """Detect Yarn Berry from repository-owned evidence only."""

    for directory in _unique_paths(root / target_path, root):
        if (directory / _YARN_MODERN_MARKER).is_file():
            return True
        major = _declared_yarn_major(directory / "package.json")
        if major is not None:
            return major >= 2
    return False


def _unique_paths(*paths: Path) -> tuple[Path, ...]:
    seen: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.append(resolved)
    return tuple(seen)


def _declared_yarn_major(manifest: Path) -> int | None:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    declared = payload.get("packageManager") if isinstance(payload, dict) else None
    if not isinstance(declared, str) or not declared.startswith("yarn@"):
        return None
    version = declared.removeprefix("yarn@").lstrip("^~>=<v ")
    major = version.split(".", 1)[0].strip()
    return int(major) if major.isdigit() else None


def _status(executor: Executor, repository: Path) -> str | None:
    result = executor.run(
        ["git", "-C", str(repository), "status", "--porcelain=v1", "-z"],
        timeout=60,
        env=_sanitized_environment(os.environ),
    )
    return result.stdout if result.ok else None


def _sanitized_environment(
    source: Mapping[str, str],
    *,
    extra: tuple[tuple[str, str], ...] = (),
) -> dict[str, str]:
    result = {
        key: value
        for key, value in source.items()
        if key in _ALLOWED_ENVIRONMENT
        and not any(marker in key.upper() for marker in _SECRET_MARKERS)
    }
    result["HOME"] = str(Path(tempfile.gettempdir()) / "touchstone-validation-home")
    for name, value in extra:
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            continue
        result[name] = value
    return result


__all__ = [
    "PreparationPolicyError",
    "PreparationReport",
    "ValidationCommand",
    "ValidationReport",
    "ValidationResult",
    "affected_validation_targets",
    "preparation_directories",
    "preparation_lockfiles",
    "prepare",
    "run_gate",
    "validate",
    "validate_affected",
    "validate_commands",
]
