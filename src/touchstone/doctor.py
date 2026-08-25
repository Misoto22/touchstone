"""Read-only installation diagnostics with stable machine-readable results."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

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


class DoctorActions(Protocol):
    def actions_secret_names(self) -> set[str]: ...

    def installation(self) -> dict[str, object] | None: ...

    def workflow(self, name: str = "touchstone.yml") -> dict[str, object] | None: ...

    def repository_info(self) -> dict[str, object] | None: ...

    def environment(self, name: str) -> dict[str, object] | None: ...


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
    actions: DoctorActions | None = None


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
    actions = None
    if not offline and config.forge.slug and "gh" in commands:
        from touchstone.hosted.github_api import GitHubCLI

        actions = GitHubCLI(config.forge.slug)
    return DoctorContext(
        commands,
        forge,
        scheduler,
        executor,
        scheduler_status,
        online=not offline and bool(config.forge.slug),
        scheduler_error=scheduler_error,
        actions=actions,
    )


#: The oldest `gh` known to complete `pr edit`. Older releases still request the
#: sunset Projects (classic) `projectCards` field, which GitHub now answers with
#: a hard error, so labelling a published pull request fails while every other
#: `gh` call this project makes keeps working.
_GH_LABEL_FLOOR = (2, 64)


def _gh_version(executor: Executor | None) -> tuple[int, ...] | None:
    if executor is None:
        return None
    result = executor.run(["gh", "--version"], timeout=30)
    if not result.ok:
        return None
    found = re.search(r"gh version (\d+)\.(\d+)\.(\d+)", result.stdout)
    return tuple(int(part) for part in found.groups()) if found else None


def _gh_version_check(context: DoctorContext) -> CheckResult:
    """Report a `gh` old enough to break publication labelling.

    A version floor cannot promise the opposite. GitHub sunsets API fields on
    its own schedule, and the next one will break a different subcommand at a
    different version, so this check says what is known to be broken rather
    than declaring any version safe.
    """

    version = _gh_version(context.executor)
    if version is None:
        return CheckResult(
            "gh.version",
            "WARN",
            "could not read the 'gh' version",
            "Run 'gh --version'; a release older than "
            f"{_GH_LABEL_FLOOR[0]}.{_GH_LABEL_FLOOR[1]} cannot label a published pull request.",
        )
    rendered = ".".join(str(part) for part in version)
    if version < _GH_LABEL_FLOOR:
        return CheckResult(
            "gh.version",
            "FAIL",
            f"gh {rendered} cannot complete 'pr edit'; publication cannot label a pull request",
            "Upgrade the 'gh' CLI, for example with 'brew upgrade gh' or your "
            "platform's package manager.",
        )
    return CheckResult(
        "gh.version",
        "PASS",
        f"gh {rendered} is newer than the releases known to fail 'pr edit'; "
        "a future GitHub API sunset can still break a subcommand",
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
    checks.extend(_schedule_checks(config))
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
    if "gh" in context.commands:
        checks.append(_gh_version_check(context))

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
    checks.extend(_actions_checks(config, context))
    return DoctorReport(tuple(checks))


def _actions_checks(config: Config, context: DoctorContext) -> list[CheckResult]:
    import re

    path = config.repo_path / ".github" / "workflows" / "touchstone.yml"
    if not path.is_file():
        return [
            CheckResult(
                "actions.workflow",
                "WARN",
                "GitHub-hosted execution is not installed",
                "Run 'touchstone actions init', review the diff, and commit it.",
            )
        ]
    text = path.read_text(encoding="utf-8")
    first_party = re.search(r"uses:\s*Misoto22/touchstone@([0-9a-f]{40})\s*$", text, re.M)
    references = re.findall(r"^\s*uses:\s*[^@\s]+@([^\s]+)\s*$", text, re.M)
    immutable = bool(references) and all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in references)
    checks = [
        CheckResult(
            "actions.pins",
            "PASS" if immutable else "FAIL",
            "every Action reference uses an immutable commit SHA"
            if immutable
            else "one or more Action references are mutable or malformed",
            None if immutable else "Regenerate with 'touchstone actions init'.",
        )
    ]
    if first_party is None:
        checks.append(
            CheckResult(
                "actions.workflow",
                "FAIL",
                "Touchstone Action reference is missing or mutable",
                "Regenerate with an immutable --action-sha.",
            )
        )
    else:
        from touchstone.hosted.workflow import ActionPins, actions_diff, render_workflow

        rendered = render_workflow(config, ActionPins(), action_sha=first_party.group(1))
        drift = actions_diff(config.repo_path, rendered)
        checks.append(
            CheckResult(
                "actions.workflow",
                "FAIL" if drift.changed else "PASS",
                "repository-owned workflow is current"
                if not drift.changed
                else "repository-owned workflow has drifted from configuration",
                "Review 'touchstone actions init --check', then regenerate."
                if drift.changed
                else None,
            )
        )
    checks.extend(
        [
            CheckResult(
                "actions.pr_only",
                "PASS"
                if not config.actions.auto_merge and "auto-merge" not in text.lower()
                else "FAIL",
                "hosted publication is PR-only and auto-merge is disabled"
                if not config.actions.auto_merge and "auto-merge" not in text.lower()
                else "hosted publication is not demonstrably PR-only",
                None
                if not config.actions.auto_merge and "auto-merge" not in text.lower()
                else "Set actions.auto_merge=false and regenerate the workflow.",
            ),
            CheckResult(
                "actions.retention",
                "PASS" if 1 <= config.actions.artifact_retention_days <= 90 else "FAIL",
                f"encrypted artifact retention: {config.actions.artifact_retention_days} days",
                None
                if 1 <= config.actions.artifact_retention_days <= 90
                else "Set actions.artifact_retention_days between 1 and 90.",
            ),
        ]
    )
    if context.actions is None:
        checks.append(
            CheckResult(
                "actions.remote",
                "WARN",
                "remote hosted configuration was not checked",
                "Authenticate gh and rerun doctor without --offline.",
            )
        )
        return checks

    workflow = context.actions.workflow()
    workflow_active = isinstance(workflow, dict) and workflow.get("state") == "active"
    checks.append(
        CheckResult(
            "actions.remote",
            "PASS" if workflow_active else "FAIL",
            "GitHub workflow is enabled"
            if workflow_active
            else "GitHub workflow is absent or disabled",
            None if workflow_active else "Push the workflow and enable Actions for the repository.",
        )
    )
    repository = context.actions.repository_info()
    expected_private = config.actions.visibility == "private"
    visibility_matches = (
        isinstance(repository, dict) and repository.get("private") is expected_private
    )
    checks.append(
        CheckResult(
            "actions.visibility",
            "PASS" if visibility_matches else "FAIL",
            f"repository visibility matches configured {config.actions.visibility} cadence"
            if visibility_matches
            else "repository visibility and configured hosted cadence disagree",
            None
            if visibility_matches
            else "Correct actions.visibility and regenerate the workflow.",
        )
    )
    checks.append(_actions_schedule_inactivity_check(repository))
    # Every member of the engine pool needs its own credential present, because
    # a Loop naming a member that has none fails at its first model call, in a
    # hosted run, hours after the configuration said it was ready.
    engine_secrets = {engine.key_env: engine for engine in config.engines.values()}
    required_secrets = set(engine_secrets) | {
        "TOUCHSTONE_APP_ID",
        "TOUCHSTONE_APP_PRIVATE_KEY",
        "TOUCHSTONE_STATE_KEY",
    }
    missing_secrets = sorted(required_secrets - context.actions.actions_secret_names())
    checks.append(
        CheckResult(
            "actions.secrets",
            "PASS" if not missing_secrets else "FAIL",
            "required Actions secret metadata exists"
            if not missing_secrets
            else f"missing Actions secret metadata: {', '.join(missing_secrets)}",
            None if not missing_secrets else _secret_remediation(missing_secrets, engine_secrets),
        )
    )
    installation = _actions_setup_attestation(config)
    attested_only = installation is not None
    if installation is None:
        legacy_reader = getattr(context.actions, "installation", None)
        if callable(legacy_reader):
            try:
                installation = legacy_reader()
            except TypeError:
                installation = None
    from touchstone.hosted.app_setup import permissions_are_exact

    app_ok = (
        isinstance(installation, dict)
        and installation.get("repository_selection") == "selected"
        and permissions_are_exact(installation.get("permissions"))
    )
    if app_ok and attested_only:
        checked_at = str(installation.get("_attested_at") or "setup time")
        checks.append(
            CheckResult(
                "actions.app",
                "WARN",
                f"publishing App matched the exact required scope at {checked_at}; "
                "live state was not reverified",
                "Run the hosted workflow to prove the current App installation can mint a token.",
            )
        )
    else:
        checks.append(
            CheckResult(
                "actions.app",
                "PASS" if app_ok else "FAIL",
                "publishing App is selected-repository scoped with the exact permissions"
                if app_ok
                else "publishing App installation scope or permissions do not match exactly",
                None if app_ok else "Rerun 'touchstone actions setup --check' and repair the App.",
            )
        )
    if config.actions.approval_environment:
        environment = context.actions.environment(config.actions.approval_environment)
        checks.append(
            CheckResult(
                "actions.environment",
                "PASS" if environment else "FAIL",
                f"publish Environment {config.actions.approval_environment!r} exists"
                if environment
                else f"publish Environment {config.actions.approval_environment!r} is missing",
                None
                if environment
                else "Create the configured Environment in repository settings.",
            )
        )
    return checks


def _secret_remediation(missing: list[str], engines: dict[str, Any]) -> str:
    """Name the exact command that would store each missing model credential.

    Printed rather than run. Touchstone never reads the operator's secret
    store: resolving a reference here would pull a credential value into this
    process, and a process that has never held one cannot leak one.
    """

    lines: list[str] = []
    for name in missing:
        engine = engines.get(name)
        reference = getattr(engine, "api_key_ref", "") if engine is not None else ""
        if reference:
            lines.append(f"op read {reference!r} | gh secret set {name} --app actions")
        elif engine is not None:
            lines.append(f"gh secret set {name} --app actions")
    if not lines:
        return "Run 'touchstone actions setup' and add the model key."
    return "Run 'touchstone actions setup', then: " + "; ".join(lines)


def _actions_setup_attestation(config: Config) -> dict[str, object] | None:
    path = Path(config.state_dir).expanduser().resolve() / "actions-setup.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("repository") != config.forge.slug
        or payload.get("state") != "complete"
        or not isinstance(payload.get("installation_id"), int)
        or not isinstance(payload.get("permissions"), dict)
    ):
        return None
    return {
        "id": payload["installation_id"],
        "app_id": payload.get("app_id"),
        "repository_selection": payload.get("repository_selection"),
        "permissions": payload["permissions"],
        "_attested_at": payload.get("updated_at", ""),
    }


def _actions_schedule_inactivity_check(
    repository: dict[str, object] | None,
    *,
    now: datetime | None = None,
) -> CheckResult:
    if not isinstance(repository, dict):
        return CheckResult(
            "actions.schedule_inactivity",
            "WARN",
            "public schedule inactivity could not be assessed",
            "Check repository activity and the workflow state on GitHub.",
        )
    if repository.get("private") is True:
        return CheckResult(
            "actions.schedule_inactivity",
            "PASS",
            "the public-repository schedule inactivity cutoff does not apply",
        )

    pushed_at = repository.get("pushed_at")
    if not isinstance(pushed_at, str):
        return CheckResult(
            "actions.schedule_inactivity",
            "WARN",
            "latest repository push time is unavailable",
            "Check repository activity and the workflow state on GitHub.",
        )
    try:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return CheckResult(
            "actions.schedule_inactivity",
            "WARN",
            "latest repository push time is malformed",
            "Check repository activity and the workflow state on GitHub.",
        )
    if pushed.tzinfo is None:
        pushed = pushed.replace(tzinfo=UTC)
    age_days = max(0, int(((now or datetime.now(UTC)) - pushed).total_seconds() // 86400))
    if age_days < 45:
        return CheckResult(
            "actions.schedule_inactivity",
            "PASS",
            f"latest repository push was {age_days} days ago",
        )

    return CheckResult(
        "actions.schedule_inactivity",
        "WARN",
        (
            f"latest repository push was {age_days} days ago; GitHub can disable "
            "public scheduled workflows after 60-day inactivity"
        ),
        (
            "Confirm repository activity before the cutoff; if scheduling is disabled, "
            "re-enable the workflow or recover with workflow_dispatch."
        ),
    )


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


def _schedule_checks(config: Config) -> list[CheckResult]:
    import datetime as dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(config.timezone)
        timezone_valid = True
    except ZoneInfoNotFoundError:
        timezone_valid = False
    checks = [
        CheckResult(
            "schedule.timezone",
            "PASS" if timezone_valid else "FAIL",
            f"repository timezone: {config.timezone}"
            if timezone_valid
            else f"unknown IANA timezone: {config.timezone}",
            None if timezone_valid else "Set timezone to a valid IANA name.",
        )
    ]
    path = Path(config.state_dir) / "due.sqlite"
    if not path.is_file():
        checks.append(
            CheckResult(
                "schedule.state",
                "WARN",
                "no durable Due Slot state exists yet",
                "Run 'touchstone run-due' once or install a Wake Signal.",
            )
        )
        return checks
    from touchstone.scheduling.store import DueStore

    records = DueStore(path).records()
    active = [
        record
        for record in records
        if record.claim_expires_at is not None and record.claim_expires_at > dt.datetime.now(dt.UTC)
    ]
    latest = records[0] if records else None
    detail = "durable Due Slot state is readable"
    if latest is not None:
        detail += f"; last {latest.loop_id}={latest.outcome or 'claimed'}"
        if latest.attempts > 1:
            detail += f" after {latest.attempts} attempts"
        if latest.missed_count:
            detail += f"; coalesced {latest.missed_count} periods"
    checks.append(CheckResult("schedule.state", "PASS", detail))
    checks.append(
        CheckResult(
            "schedule.claim",
            "WARN" if active else "PASS",
            f"active claim owned by {active[0].claim_owner}"
            if active
            else "no active durable claim",
            "Wait for the claim to expire before manual recovery." if active else None,
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
    "_actions_checks",
    "build_context",
    "run_doctor",
]
