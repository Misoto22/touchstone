from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.test_config import _valid_config, _write
from touchstone.cli import main
from touchstone.config import load_config
from touchstone.discovery import ProjectDiscovery
from touchstone.doctor import DoctorContext, run_doctor
from touchstone.execution.base import Result
from touchstone.execution.local import LocalExecutor
from touchstone.initialize import InitOptions, initialize
from touchstone.scheduling.base import SchedulerStatus


class MemoryForge:
    def __init__(self, *, labels: set[str] | None = None) -> None:
        self._labels = labels or set()

    def repository_info(self) -> dict[str, object]:
        return {
            "nameWithOwner": "acme/widgets",
            "defaultBranchRef": {"name": "main"},
            "autoMergeAllowed": True,
        }

    def labels(self) -> set[str]:
        return set(self._labels)

    def branch_protection(self, branch: str) -> bool:
        return True

    def latest_run(self, workflow: str, *, branch: str | None = None) -> str:
        return "success"


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _valid_config().replace('path = "."', 'path = "repo"')
    text = text.replace(
        'slug = "acme/widgets"',
        'slug = "acme/widgets"\nrequired_workflows = ["ci.yml"]',
    )
    return load_config(_write(tmp_path / "touchstone.toml", text))


def test_doctor_fails_before_sessions_when_engine_is_missing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    context = DoctorContext(
        commands=frozenset({"git", "gh"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    report = run_doctor(config, context)
    check = report.by_id("engine.command")

    assert (check.level, check.repair) == (
        "FAIL",
        "Install the configured 'codex' command and authenticate it.",
    )
    assert report.exit_code == 1


def test_doctor_json_contains_stable_checks_and_no_environment(tmp_path: Path) -> None:
    config = _config(tmp_path)
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    payload = json.loads(run_doctor(config, context).to_json())

    assert payload["exit_code"] == 0
    assert payload["checks"][0]["id"] == "config.schema"
    assert "environment" not in json.dumps(payload).lower()


def test_doctor_command_is_registered(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path)

    code = main(["--config", str(config.source.path), "doctor", "--json", "--offline"])

    output = json.loads(capsys.readouterr().out)
    assert code in {0, 1}
    assert output["checks"][0]["id"] == "config.schema"


def test_doctor_fails_when_unattended_health_has_no_workflow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = load_config(
        _write(
            tmp_path / "touchstone.toml",
            _valid_config().replace('path = "."', 'path = "repo"'),
        )
    )
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    report = run_doctor(config, context)

    assert report.by_id("workflows.required").level == "FAIL"
    assert report.exit_code == 1


def test_doctor_fails_when_no_model_is_configured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    object.__setattr__(config.engine, "model", "")
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    assert run_doctor(config, context).by_id("engine.model").level == "FAIL"


def test_doctor_checks_the_target_is_a_git_repo_with_origin(tmp_path: Path) -> None:
    config = _config(tmp_path)
    subprocess.run(["git", "-C", str(config.repo_path), "init"], check=True, capture_output=True)
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        executor=LocalExecutor(),
    )

    report = run_doctor(config, context)

    assert report.by_id("project.git").level == "PASS"
    assert report.by_id("project.origin").level == "FAIL"
    assert report.by_id("project.worktrees").level == "PASS"


def test_doctor_reports_scheduled_loops_that_are_not_installed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        scheduler_status=SchedulerStatus(
            adapter="launchd",
            supported=True,
            missing=(tmp_path / "io.touchstone.agent.code.plist",),
        ),
    )

    check = run_doctor(config, context).by_id("scheduler.installed")

    assert check.level == "WARN"
    assert "1" in check.summary


def test_doctor_names_deprecated_overrides_without_logging_values(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TOUCHSTONE_MODEL", "private-model-value")
    config = _config(tmp_path)
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    check = run_doctor(config, context).by_id("config.deprecated_env")

    assert check.level == "WARN"
    assert "TOUCHSTONE_MODEL" in check.summary
    assert "private-model-value" not in check.summary


def test_doctor_fails_when_default_branch_drifted(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class DriftedForge(MemoryForge):
        def repository_info(self) -> dict[str, object]:
            info = super().repository_info()
            info["defaultBranchRef"] = {"name": "trunk"}
            return info

    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=DriftedForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )

    check = run_doctor(config, context).by_id("forge.default_branch")

    assert check.level == "FAIL"
    assert "trunk" in check.summary


def test_doctor_discovers_commands_on_the_execution_target(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from touchstone import doctor

    class TargetExecutor:
        where = "ssh audit.example"

        def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
            command = argv[-1]
            present = command in {"git", "codex"}
            return Result(0 if present else 1, f"/usr/bin/{command}\n" if present else "", "")

    class Scheduler:
        def status(self, _config):  # type: ignore[no-untyped-def]
            return SchedulerStatus(adapter="launchd", supported=True)

    executor = TargetExecutor()
    monkeypatch.setattr(doctor.execution, "build", lambda _config: executor)
    monkeypatch.setattr(doctor, "current_scheduler", lambda _executor: Scheduler())

    context = doctor.build_context(_config(tmp_path), offline=True)

    assert context.commands == frozenset({"git", "codex"})


def test_online_doctor_fails_when_github_repository_is_inaccessible(tmp_path: Path) -> None:
    class InaccessibleForge(MemoryForge):
        def repository_info(self) -> None:
            return None

    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=InaccessibleForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        online=True,
    )

    check = run_doctor(_config(tmp_path), context).by_id("forge.repository")

    assert check.level == "FAIL"


def test_online_doctor_fails_when_setup_labels_are_missing(tmp_path: Path) -> None:
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels=set()),
        scheduler="launchd",
        online=True,
    )

    check = run_doctor(_config(tmp_path), context).by_id("forge.labels")

    assert check.level == "FAIL"


def test_doctor_warns_when_default_branch_protection_is_missing(tmp_path: Path) -> None:
    class UnprotectedForge(MemoryForge):
        def branch_protection(self, branch: str) -> bool:
            return False

    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=UnprotectedForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        online=True,
    )

    check = run_doctor(_config(tmp_path), context).by_id("forge.branch_protection")

    assert check.level == "WARN"
    assert "not confirmed" in check.summary


def test_doctor_reports_scheduler_inspection_failure(tmp_path: Path) -> None:
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        scheduler_error="could not locate executable",
    )

    check = run_doctor(_config(tmp_path), context).by_id("scheduler.installed")

    assert check.level == "WARN"
    assert "could not inspect" in check.summary


def _v2_config(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = tmp_path / "v2-repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"name":"v2-repo","dependencies":{"next":"15.0.0","react":"19.0.0"}}',
        encoding="utf-8",
    )
    report = initialize(
        InitOptions(
            start=repo,
            engine="codex",
            model="gpt-test",
            workflows=("ci.yml",),
            discovered=ProjectDiscovery(repo, "acme/v2-repo", "main", ("codex",), "launchd"),
        ),
        LocalExecutor(),
    )
    config = load_config(report.root)
    object.__setattr__(config, "state_dir", tmp_path / "touchstone-state")
    return report, config


def _v2_context() -> DoctorContext:
    return DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
    )


def test_doctor_checks_generated_profile_provenance(tmp_path: Path) -> None:
    report, config = _v2_config(tmp_path)

    assert run_doctor(config, _v2_context()).by_id("profile.provenance").level == "PASS"

    report.generated.write_text(
        report.generated.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
    )
    edited = load_config(report.root)
    check = run_doctor(edited, _v2_context()).by_id("profile.generated")
    assert check.level == "FAIL"
    assert "refresh" in check.repair


def test_doctor_reports_missing_targets_and_unsupported_versions(tmp_path: Path) -> None:
    report, config = _v2_config(tmp_path)
    object.__setattr__(config.targets["v2-repo"], "path", Path("missing"))
    assert run_doctor(config, _v2_context()).by_id("target.v2-repo.path").level == "FAIL"

    (report.root.parent / "package.json").write_text(
        '{"name":"v2-repo","dependencies":{"next":"99.0.0","react":"19.0.0"}}',
        encoding="utf-8",
    )
    fresh = load_config(report.root)
    unsupported = run_doctor(fresh, _v2_context()).by_id("profile.unsupported")
    assert unsupported.level == "WARN"
    assert "nextjs" in unsupported.summary


def test_doctor_reports_unresolved_package_manager_evidence(tmp_path: Path) -> None:
    report, config = _v2_config(tmp_path)
    (report.root.parent / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (report.root.parent / "yarn.lock").write_text("# yarn\n", encoding="utf-8")

    check = run_doctor(config, _v2_context()).by_id("profile.package_manager")

    assert check.level == "WARN"
    assert "npm/yarn" in check.summary


class _GhExecutor:
    """Answer only the version probe doctor makes."""

    def __init__(self, output: str, *, ok: bool = True) -> None:
        self.output = output
        self.ok = ok

    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        assert argv == ["gh", "--version"]
        return SimpleNamespace(ok=self.ok, stdout=self.output, stderr="", code=0 if self.ok else 1)


def _gh_check(output: str, *, ok: bool = True):  # type: ignore[no-untyped-def]
    from touchstone.doctor import DoctorContext, _gh_version_check

    context = DoctorContext(
        commands=frozenset({"gh"}),
        forge=MemoryForge(),
        scheduler="launchd",
        executor=_GhExecutor(output, ok=ok),
    )
    return _gh_version_check(context)


def test_a_gh_that_cannot_label_a_pull_request_fails_doctor() -> None:
    check = _gh_check("gh version 2.63.2 (2024-12-05)\n")

    assert check.level == "FAIL"
    assert "2.63.2" in check.summary
    assert "pr edit" in check.summary
    assert check.repair is not None and "gh" in check.repair


def test_a_supported_gh_passes_without_promising_future_versions() -> None:
    check = _gh_check("gh version 2.98.0 (2026-08-20)\n")

    assert check.level == "PASS"
    # A version floor cannot promise the next API sunset will not break something.
    assert "future GitHub API sunset" in check.summary


def test_an_unreadable_gh_version_warns_rather_than_passing() -> None:
    assert _gh_check("", ok=False).level == "WARN"
    assert _gh_check("something unexpected\n").level == "WARN"


def test_the_floor_is_the_first_release_that_completes_pr_edit() -> None:
    assert _gh_check("gh version 2.64.0 (2025-01-01)\n").level == "PASS"
    assert _gh_check("gh version 2.63.99 (2024-12-31)\n").level == "FAIL"
