"""One repository per container, and one clock."""

from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.execution.container import SupervisorReport, supervise

ROOT = Path(__file__).resolve().parents[1]


def test_the_supervisor_wakes_run_due_on_its_interval() -> None:
    woken: list[int] = []
    slept: list[float] = []

    report = supervise(
        run=lambda: woken.append(len(woken)) or 0,  # type: ignore[func-returns-value]
        sleep=slept.append,
        interval_seconds=900,
        iterations=3,
    )

    assert len(woken) == 3
    assert slept == [900.0, 900.0, 900.0]
    assert report.woken == 3
    assert report.exit_code == 0


def test_a_failing_wake_does_not_end_the_supervisor() -> None:
    outcomes = iter([1, 0, 1])

    report = supervise(
        run=lambda: next(outcomes),
        sleep=lambda _seconds: None,
        interval_seconds=60,
        iterations=3,
    )

    # A loop that stopped on its first failure would need a person to restart
    # it, which is the opposite of what a scheduled backend is for. The count
    # is reported so a supervisor that never succeeds is still visible.
    assert report.woken == 3
    assert report.failed == 2
    assert report.exit_code == 0


def test_a_raised_error_is_counted_rather_than_escaping() -> None:
    def explode() -> int:
        raise RuntimeError("the run blew up")

    report = supervise(run=explode, sleep=lambda _seconds: None, interval_seconds=60, iterations=2)

    assert report.failed == 2
    assert report.exit_code == 0


def test_an_interval_below_a_minute_is_refused() -> None:
    with pytest.raises(ValueError, match="interval"):
        supervise(run=lambda: 0, sleep=lambda _s: None, interval_seconds=30, iterations=1)


def test_the_supervisor_reports_what_it_did() -> None:
    assert SupervisorReport(woken=4, failed=1).exit_code == 0


def test_the_image_carries_no_project_toolchain() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    # A fat image is always missing the exact version a repository needs, and one
    # of the stacks Touchstone supports cannot run on Linux at all. The
    # repository answers what it needs by deriving from this image.
    for absent in ("cargo", "dotnet", "openjdk", "ruby"):
        assert absent not in dockerfile.lower()
    assert "git" in dockerfile.lower()


def test_the_image_installs_the_agent_cli_from_its_committed_lock() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "agent-runtime" in dockerfile
    assert "npm ci" in dockerfile


def test_a_compose_service_per_member_with_its_own_state_volume(tmp_path: Path) -> None:
    from tests.test_fleet import PROJECT, _project
    from touchstone.fleet import load_project, render_compose

    project = load_project(_project(tmp_path, PROJECT))

    compose = render_compose(project)

    assert "acme-widgets" in compose
    assert "acme-gadgets" in compose
    # One state volume per service: a shared one would let a failure in one
    # repository's state reach another's, which is the whole reason the
    # containers are separate.
    assert compose.count("touchstone-state-") >= 2
    # Variable names are the point of the file; values and vault references are
    # what it must never carry. `test_the_compose_file_carries_references_and_
    # never_values` holds that line.
    assert "ANTHROPIC_API_KEY:" not in compose


def test_the_compose_file_carries_references_and_never_values(tmp_path: Path) -> None:
    from tests.test_fleet import PROJECT, _project
    from touchstone.fleet import load_project, render_compose

    project = load_project(_project(tmp_path, PROJECT))

    compose = render_compose(project)

    assert "op://" not in compose
    assert "sk-" not in compose


def test_a_container_backed_loop_may_not_auto_merge() -> None:
    from touchstone.lifecycle import auto_merge_unsupported

    # A container runs the local publish path, whose Verify shares the process
    # that held the model credential. Task 4's refusal covers it without a
    # container-specific rule, and this states that it does.
    assert "policy-unsupported" in auto_merge_unsupported(
        requested=True, independently_verified=False
    )


def test_each_service_names_the_variables_its_env_file_must_hold(tmp_path: Path) -> None:
    from tests.test_fleet import PROJECT, _project
    from touchstone.fleet import load_project, render_compose

    body = PROJECT.replace(
        "[defaults.engine.cheap]",
        '[defaults.engine.cheap]\napi_key_env = "CHEAP_API_KEY"',
    )
    project = load_project(_project(tmp_path, body))

    compose = render_compose(project)

    # Without this the operator has to read the project file to learn what the
    # env_file must contain, and a missing variable surfaces as a model call
    # failing inside a container hours later.
    widgets = compose.split("  acme-widgets:", 1)[1]
    assert "CHEAP_API_KEY" in widgets
    assert "GH_TOKEN" in widgets
    # A member that takes no loop on the cheap engine is not told to hold its key.
    gadgets = compose.split("  acme-gadgets:", 1)[1].split("volumes:", 1)[0]
    assert "CHEAP_API_KEY" in gadgets


def test_a_member_taking_no_loop_on_an_engine_is_not_asked_for_its_key(tmp_path: Path) -> None:
    from tests.test_fleet import PROJECT, _project
    from touchstone.fleet import load_project, render_compose

    body = PROJECT.replace(
        "[defaults.engine.cheap]",
        '[defaults.engine.cheap]\napi_key_env = "CHEAP_API_KEY"',
    ).replace('engine = "cheap"', "")
    project = load_project(_project(tmp_path, body))

    compose = render_compose(project)

    assert "CHEAP_API_KEY" not in compose


def test_a_volume_path_survives_the_project_being_cloned_elsewhere(tmp_path: Path) -> None:
    from tests.test_fleet import PROJECT, _project
    from touchstone.fleet import load_project, render_compose

    project = load_project(_project(tmp_path, PROJECT))

    compose = render_compose(project, base=tmp_path / "projects")

    # Compose resolves a relative path against the file's own directory, so an
    # absolute one pins the fleet to the machine that rendered it.
    assert str(tmp_path) not in compose
    assert "../widgets:/repository" in compose


def test_an_absolute_path_is_kept_when_it_cannot_be_made_relative(tmp_path: Path) -> None:
    from tests.test_fleet import PROJECT, _project
    from touchstone.fleet import load_project, render_compose

    project = load_project(_project(tmp_path, PROJECT))

    compose = render_compose(project, base=Path("/"))

    assert ":/repository" in compose
