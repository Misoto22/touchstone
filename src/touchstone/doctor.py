"""Read-only installation diagnostics with stable machine-readable results."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

from touchstone import execution
from touchstone.config import Config
from touchstone.forge import Forge

Level = Literal["PASS", "WARN", "FAIL"]


class DoctorForge(Protocol):
    def repository_info(self) -> dict[str, object] | None: ...

    def labels(self) -> set[str]: ...

    def latest_run(self, workflow: str, *, branch: str | None = None) -> str: ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    level: Level
    summary: str
    repair: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    def by_id(self, check_id: str) -> CheckResult:
        return next(check for check in self.checks if check.id == check_id)

    @property
    def exit_code(self) -> int:
        return 1 if any(check.level == "FAIL" for check in self.checks) else 0

    def to_json(self) -> str:
        return json.dumps(
            {"exit_code": self.exit_code, "checks": [asdict(check) for check in self.checks]},
            indent=2,
        )


@dataclass(frozen=True, slots=True)
class DoctorContext:
    commands: frozenset[str]
    forge: DoctorForge
    scheduler: Literal["launchd", "systemd", "unsupported"]


class _OfflineForge:
    def repository_info(self) -> None:
        return None

    def labels(self) -> set[str]:
        return set()

    def latest_run(self, workflow: str, *, branch: str | None = None) -> str:
        return "unknown"


def build_context(config: Config, *, offline: bool = False) -> DoctorContext:
    commands = frozenset(name for name in ("git", "gh", "codex", "claude") if shutil.which(name))
    if offline or not config.forge.slug:
        forge: DoctorForge = _OfflineForge()
    else:
        forge = Forge(config.forge.slug, execution.build(config))
    if sys.platform == "darwin":
        scheduler = "launchd"
    elif sys.platform.startswith("linux"):
        scheduler = "systemd"
    else:
        scheduler = "unsupported"
    return DoctorContext(commands, forge, scheduler)


def run_doctor(config: Config, context: DoctorContext) -> DoctorReport:
    checks: list[CheckResult] = [
        CheckResult("config.schema", "PASS", "configuration schema version 1 is valid")
    ]
    checks.append(
        CheckResult(
            "project.path",
            "PASS" if config.repo_path.is_dir() else "FAIL",
            f"project path: {config.repo_path}",
            None
            if config.repo_path.is_dir()
            else "Run 'touchstone init' inside the target repository.",
        )
    )
    for command in ("git", "gh"):
        present = command in context.commands
        checks.append(
            CheckResult(
                f"{command}.command",
                "PASS" if present else "FAIL",
                f"'{command}' is {'available' if present else 'missing'}",
                None
                if present
                else f"Install the '{command}' command and make it available on PATH.",
            )
        )

    engine_present = config.engine.name in context.commands
    checks.append(
        CheckResult(
            "engine.command",
            "PASS" if engine_present else "FAIL",
            f"configured engine '{config.engine.name}' is "
            + ("available" if engine_present else "missing"),
            None
            if engine_present
            else f"Install the configured '{config.engine.name}' command and authenticate it.",
        )
    )

    repository = context.forge.repository_info()
    checks.append(
        CheckResult(
            "forge.repository",
            "PASS" if repository else "WARN",
            f"GitHub repository {config.forge.slug} is accessible"
            if repository
            else "GitHub repository access was not checked",
            None if repository else "Run without --offline after authenticating 'gh'.",
        )
    )
    auto_merge = bool(repository and repository.get("autoMergeAllowed"))
    checks.append(
        CheckResult(
            "forge.auto_merge",
            "PASS" if auto_merge else "WARN",
            "GitHub auto-merge is enabled" if auto_merge else "GitHub auto-merge is not confirmed",
            None
            if auto_merge
            else "Enable auto-merge in the repository settings before unattended runs.",
        )
    )

    for workflow in config.forge.required_workflows:
        conclusion = context.forge.latest_run(workflow, branch=config.forge.default_branch)
        level: Level = (
            "PASS"
            if conclusion == "success"
            else ("FAIL" if conclusion == "failure" else "WARN")
        )
        checks.append(
            CheckResult(
                f"workflow.{workflow}",
                level,
                f"{workflow} latest conclusion: {conclusion}",
                None if level == "PASS" else f"Run and repair '{workflow}' on the default branch.",
            )
        )
    if not config.forge.required_workflows:
        checks.append(
            CheckResult(
                "workflows.required",
                "WARN",
                "no required default-branch workflows are configured",
                "Add forge.required_workflows before enabling unattended runs.",
            )
        )

    expected_labels = _labels(config)
    present_labels = context.forge.labels()
    missing_labels = [label for label in expected_labels if label not in present_labels]
    checks.append(
        CheckResult(
            "forge.labels",
            "PASS" if not missing_labels else "WARN",
            "configured labels exist"
            if not missing_labels
            else f"missing labels: {', '.join(missing_labels)}",
            None if not missing_labels else "Run 'touchstone setup'.",
        )
    )

    writable = _nearest_existing(config.state_dir).is_dir() and os.access(
        _nearest_existing(config.state_dir), os.W_OK
    )
    checks.append(
        CheckResult(
            "state.directory",
            "PASS" if writable else "FAIL",
            f"state directory can be created at {config.state_dir}"
            if writable
            else f"state directory is not writable: {config.state_dir}",
            None if writable else "Choose a writable state_dir in touchstone.toml.",
        )
    )
    checks.append(
        CheckResult(
            "scheduler.platform",
            "PASS" if context.scheduler != "unsupported" else "WARN",
            f"native scheduler: {context.scheduler}",
            None
            if context.scheduler != "unsupported"
            else "Run Touchstone from an external scheduler on this platform.",
        )
    )
    return DoctorReport(tuple(checks))


def _labels(config: Config) -> tuple[str, ...]:
    labels = [*(loop.label for loop in config.loops.values()), config.forge.escalation_label]
    return tuple(dict.fromkeys(labels))


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


__all__ = [
    "CheckResult",
    "DoctorContext",
    "DoctorReport",
    "build_context",
    "run_doctor",
]
