"""Structured, secret-scrubbed Validation Gates."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from touchstone.config import Config
from touchstone.execution import Executor

ValidationOutcome = Literal["completed", "blocked"]
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
            env=_sanitized_environment(env or os.environ),
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
        managers = target.package_managers or ("",)
        for manager in managers:
            command = _preparation_command(
                (manager,) if manager else (),
                target_id,
                allow_scripts=any(gate.allow_scripts for gate in requirements),
                allow_build_hooks=any(gate.allow_build_hooks for gate in requirements),
            )
            results.append(run_gate(root, target.path, command, executor))
    outcome: ValidationOutcome = (
        "blocked" if any(not result.ok for result in results) else "completed"
    )
    return PreparationReport(outcome, tuple(results))


def _preparation_command(
    managers: tuple[str, ...],
    target: str,
    *,
    allow_scripts: bool,
    allow_build_hooks: bool,
) -> ValidationCommand:
    if "npm" in managers:
        argv = ("npm", "ci") + (() if allow_scripts else ("--ignore-scripts",))
    elif "pnpm" in managers:
        argv = ("pnpm", "install", "--frozen-lockfile") + (
            () if allow_scripts else ("--ignore-scripts",)
        )
    elif "yarn" in managers:
        argv = ("yarn", "install", "--immutable") + (
            () if allow_scripts else ("--mode=skip-builds",)
        )
    elif "uv" in managers:
        argv = ("uv", "sync", "--frozen") + (() if allow_build_hooks else ("--no-install-project",))
    elif "poetry" in managers:
        argv = ("poetry", "install") + (() if allow_build_hooks else ("--no-root",))
    elif "pdm" in managers:
        argv = ("pdm", "sync", "--frozen-lockfile") + (() if allow_build_hooks else ("--no-self",))
    else:
        raise ValueError("locked preparation requires a confirmed package manager")
    return ValidationCommand(
        target=target,
        argv=argv,
        timeout_seconds=1200,
        capability="locked-install",
        enabled=True,
    )


def _status(executor: Executor, repository: Path) -> str | None:
    result = executor.run(
        ["git", "-C", str(repository), "status", "--porcelain=v1", "-z"],
        timeout=60,
        env=_sanitized_environment(os.environ),
    )
    return result.stdout if result.ok else None


def _sanitized_environment(source: Mapping[str, str]) -> dict[str, str]:
    result = {
        key: value
        for key, value in source.items()
        if key in _ALLOWED_ENVIRONMENT
        and not any(marker in key.upper() for marker in _SECRET_MARKERS)
    }
    result["HOME"] = str(Path(tempfile.gettempdir()) / "touchstone-validation-home")
    return result


__all__ = [
    "PreparationReport",
    "ValidationCommand",
    "ValidationReport",
    "ValidationResult",
    "prepare",
    "run_gate",
    "validate",
    "validate_commands",
]
