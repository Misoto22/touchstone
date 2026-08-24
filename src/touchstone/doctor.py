"""Read-only installation diagnostics with stable machine-readable results."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

from touchstone import execution
from touchstone.config import Config
from touchstone.execution import Executor
from touchstone.forge import Forge
from touchstone.scheduling import current_scheduler
from touchstone.scheduling.base import SchedulerStatus

Level = Literal["PASS", "WARN", "FAIL"]


class DoctorForge(Protocol):
    def repository_info(self) -> dict[str, object] | None: ...

    def branch_protection(self, branch: str) -> bool | None: ...

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
    executor: Executor | None = None
    scheduler_status: SchedulerStatus | None = None
    online: bool = False
    scheduler_error: str | None = None


class _OfflineForge:
    def repository_info(self) -> None:
        return None

    def labels(self) -> set[str]:
        return set()

    def branch_protection(self, branch: str) -> None:
        return None

    def latest_run(self, workflow: str, *, branch: str | None = None) -> str:
        return "unknown"


def build_context(config: Config, *, offline: bool = False) -> DoctorContext:
    executor = execution.build(config)
    commands = _available_commands(executor)
    if offline or not config.forge.slug:
        forge: DoctorForge = _OfflineForge()
    else:
        forge = Forge(config.forge.slug, executor)
    if sys.platform == "darwin":
        scheduler = "launchd"
    elif sys.platform.startswith("linux"):
        scheduler = "systemd"
    else:
        scheduler = "unsupported"
    scheduler_status = None
    scheduler_error = None
    if scheduler != "unsupported":
        try:
            # Scheduler files and launch commands live on the local
            # orchestrator, independently of a local or SSH execution target.
            scheduler_status = current_scheduler(execution.LocalExecutor()).status(config)
        except RuntimeError as exc:
            scheduler_error = str(exc)
    return DoctorContext(
        commands,
        forge,
        scheduler,
        executor,
        scheduler_status,
        online=not offline and bool(config.forge.slug),
        scheduler_error=scheduler_error,
    )


def _available_commands(executor: Executor) -> frozenset[str]:
    available: set[str] = set()
    for command in ("git", "gh", "codex", "claude"):
        result = executor.run(
            ["sh", "-c", 'command -v "$1"', "touchstone-doctor", command], timeout=30
        )
        if result.ok and result.stdout.strip():
            available.add(command)
    return frozenset(available)


def run_doctor(config: Config, context: DoctorContext) -> DoctorReport:
    checks: list[CheckResult] = [
        CheckResult(
            "config.schema",
            "PASS",
            f"configuration schema version {config.source.schema_version} is valid",
        )
    ]
    checks.extend(_profile_checks(config))
    deprecated = tuple(
        name
        for name in (
            "TOUCHSTONE_ENGINE",
            "TOUCHSTONE_MODEL",
            "TOUCHSTONE_EFFORT",
            "TOUCHSTONE_REVIEW_EFFORT",
            "TOUCHSTONE_TIMEOUT",
            "TOUCHSTONE_TARGET",
            "TOUCHSTONE_REPO",
            "TOUCHSTONE_STATE",
        )
        if name in os.environ
    )
    if deprecated:
        checks.append(
            CheckResult(
                "config.deprecated_env",
                "WARN",
                f"deprecated environment override(s) active: {', '.join(deprecated)}",
                "Move these values into version-1 config or stable CLI arguments.",
            )
        )
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
    if context.executor is not None:
        repo = config.execution_repo
        git_repo = context.executor.run(
            ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"], timeout=30
        )
        is_worktree = git_repo.ok and git_repo.stdout.strip() == "true"
        checks.append(
            CheckResult(
                "project.git",
                "PASS" if is_worktree else "FAIL",
                "project path is a Git worktree"
                if is_worktree
                else "project path is not a readable Git worktree",
                None if is_worktree else "Run 'touchstone init' inside the target repository.",
            )
        )
        origin = context.executor.run(
            ["git", "-C", repo, "remote", "get-url", "origin"], timeout=30
        )
        checks.append(
            CheckResult(
                "project.origin",
                "PASS" if origin.ok and origin.stdout.strip() else "FAIL",
                "origin remote is configured"
                if origin.ok and origin.stdout.strip()
                else "origin remote is missing",
                None
                if origin.ok and origin.stdout.strip()
                else "Add the authorised GitHub repository as origin.",
            )
        )
        worktrees = context.executor.run(
            ["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=30
        )
        checks.append(
            CheckResult(
                "project.worktrees",
                "PASS" if worktrees.ok else "FAIL",
                "Git worktrees are available" if worktrees.ok else "Git worktrees are unavailable",
                None if worktrees.ok else "Use a Git version with worktree support.",
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
    checks.append(
        CheckResult(
            "engine.model",
            "PASS" if config.engine.model.strip() else "FAIL",
            f"configured model: {config.engine.model}"
            if config.engine.model.strip()
            else "no model is configured",
            None
            if config.engine.model.strip()
            else "Set engine.model or rerun 'touchstone init' with an explicit model.",
        )
    )

    repository = context.forge.repository_info()
    repository_level: Level = "PASS" if repository else ("FAIL" if context.online else "WARN")
    checks.append(
        CheckResult(
            "forge.repository",
            repository_level,
            f"GitHub repository {config.forge.slug} is accessible"
            if repository
            else (
                f"GitHub repository {config.forge.slug} is not accessible"
                if context.online
                else "GitHub repository access was not checked"
            ),
            None
            if repository
            else (
                "Authenticate 'gh' and verify forge.slug."
                if context.online
                else "Run without --offline after authenticating 'gh'."
            ),
        )
    )
    if repository:
        branch_ref = repository.get("defaultBranchRef")
        live_branch = str(branch_ref.get("name") or "") if isinstance(branch_ref, dict) else ""
        matches = bool(live_branch and live_branch == config.forge.default_branch)
        checks.append(
            CheckResult(
                "forge.default_branch",
                "PASS" if matches else "FAIL",
                f"configured default branch matches GitHub ({live_branch})"
                if matches
                else (
                    f"configured default branch {config.forge.default_branch!r} "
                    f"does not match GitHub {live_branch!r}"
                ),
                None if matches else "Update forge.default_branch or rerun 'touchstone init'.",
            )
        )
        protected = context.forge.branch_protection(config.forge.default_branch)
        checks.append(
            CheckResult(
                "forge.branch_protection",
                "PASS" if protected else "WARN",
                "default branch protection is enabled"
                if protected
                else "default branch protection is not confirmed",
                None
                if protected
                else "Configure branch protection or a ruleset for the default branch.",
            )
        )
    checks.append(
        CheckResult(
            "forge.auto_merge",
            "PASS",
            "PR-only mode does not enable or require GitHub auto-merge",
        )
    )

    for workflow in config.forge.required_workflows:
        conclusion = context.forge.latest_run(workflow, branch=config.forge.default_branch)
        level: Level = (
            "PASS" if conclusion == "success" else ("FAIL" if conclusion == "failure" else "WARN")
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
                "FAIL",
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
            "PASS" if not missing_labels else ("FAIL" if context.online else "WARN"),
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
    if context.scheduler_error is not None:
        checks.append(
            CheckResult(
                "scheduler.installed",
                "WARN",
                "could not inspect configured scheduler files",
                "Verify the touchstone executable is installed and on PATH.",
            )
        )
    elif context.scheduler_status is not None:
        missing = context.scheduler_status.missing
        checks.append(
            CheckResult(
                "scheduler.installed",
                "PASS" if not missing else "WARN",
                "all configured schedules are installed"
                if not missing
                else f"{len(missing)} configured scheduler file(s) are missing",
                None if not missing else "Run 'touchstone install-scheduler'.",
            )
        )
    return DoctorReport(tuple(checks))


def _labels(config: Config) -> tuple[str, ...]:
    labels = [*(loop.label for loop in config.loops.values()), config.forge.escalation_label]
    return tuple(dict.fromkeys(labels))


def _profile_checks(config: Config) -> list[CheckResult]:
    if config.source.schema_version != 2:
        return []
    from touchstone.profiles.materialize import (
        ambiguous_package_managers,
        detect_package_managers,
        profile_diff,
    )

    checks: list[CheckResult] = []
    generated = config.source.generated_path
    generated_exists = generated is not None and generated.is_file()
    checks.append(
        CheckResult(
            "profile.generated_file",
            "PASS" if generated_exists else "FAIL",
            f"generated Profile configuration: {generated}"
            if generated_exists
            else "generated Profile configuration is missing",
            None if generated_exists else "Run 'touchstone profile refresh --write'.",
        )
    )
    for target_id, target in config.targets.items():
        path = config.repo_path / target.path
        checks.append(
            CheckResult(
                f"target.{target_id}.path",
                "PASS" if path.is_dir() else "FAIL",
                f"Target {target_id!r} path: {target.path.as_posix()}"
                if path.is_dir()
                else f"Target {target_id!r} path is missing: {target.path.as_posix()}",
                None if path.is_dir() else "Refresh Profiles or correct the Target override.",
            )
        )

    live_managers = detect_package_managers(config.repo_path)
    ambiguous = ambiguous_package_managers(live_managers)
    selected = set(
        config.generated_metadata.package_managers if config.generated_metadata is not None else ()
    )
    unresolved = tuple(group for group in ambiguous if not selected.intersection(group))
    checks.append(
        CheckResult(
            "profile.package_manager",
            "WARN" if unresolved else "PASS",
            "ambiguous package-manager evidence: "
            + ", ".join("/".join(group) for group in unresolved)
            if unresolved
            else "package-manager evidence is unambiguous or explicitly resolved",
            "Rerun 'touchstone init --package-manager NAME' or record an explicit choice."
            if unresolved
            else None,
        )
    )

    if not generated_exists:
        return checks
    drift = profile_diff(config)
    expected_digest = drift.materialized.source_digest
    actual_digest = (
        config.generated_metadata.source_digest if config.generated_metadata is not None else ""
    )
    provenance_current = bool(actual_digest and actual_digest == expected_digest)
    checks.append(
        CheckResult(
            "profile.provenance",
            "PASS" if provenance_current else "FAIL",
            "generated Profile provenance matches repository evidence"
            if provenance_current
            else "generated Profile provenance does not match repository evidence",
            None if provenance_current else "Review 'touchstone profile diff', then refresh.",
        )
    )
    checks.append(
        CheckResult(
            "profile.generated",
            "PASS" if not drift.changed else "FAIL",
            "generated Profile configuration is current"
            if not drift.changed
            else "generated Profile configuration was edited or is stale",
            None if not drift.changed else "Run 'touchstone profile refresh --write'.",
        )
    )
    unsupported = [
        match.warning or f"unsupported {match.profile}"
        for matches in drift.materialized.matches.values()
        for match in matches
        if match.verdict == "unsupported"
    ]
    checks.append(
        CheckResult(
            "profile.unsupported",
            "WARN" if unsupported else "PASS",
            "; ".join(unsupported) if unsupported else "detected Profile versions are supported",
            "Use the base Profile or add a reviewed local declarative Profile."
            if unsupported
            else None,
        )
    )
    return checks


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
