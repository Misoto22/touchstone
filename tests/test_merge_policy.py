"""Auto-merge carries a policy, not just a switch."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from touchstone.config import ConfigError, load
from touchstone.scheduling.window import WindowError, parse_windows, within_windows

SYDNEY = "Australia/Sydney"


def _at(day: str, hour: int, minute: int = 0) -> dt.datetime:
    """A UTC instant that is `day` at `hour` in Sydney."""
    from zoneinfo import ZoneInfo

    index = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"].index(day)
    # 2026-08-24 is a Monday.
    local = dt.datetime(2026, 8, 24 + index, hour, minute, tzinfo=ZoneInfo(SYDNEY))
    return local.astimezone(dt.UTC)


def test_a_weekday_range_covers_its_ends() -> None:
    windows = parse_windows(("MON-THU,09:00-17:00",))

    assert within_windows(windows, _at("MON", 9, 0), SYDNEY) is True
    assert within_windows(windows, _at("THU", 16, 59), SYDNEY) is True
    assert within_windows(windows, _at("FRI", 12, 0), SYDNEY) is False


def test_the_closing_edge_is_exclusive() -> None:
    windows = parse_windows(("MON,09:00-17:00",))

    assert within_windows(windows, _at("MON", 16, 59), SYDNEY) is True
    # A window that ends at 17:00 and includes 17:00 is a window nobody can
    # write as "until five".
    assert within_windows(windows, _at("MON", 17, 0), SYDNEY) is False


def test_a_single_day_is_a_range_of_one() -> None:
    windows = parse_windows(("WED,10:00-11:00",))

    assert within_windows(windows, _at("WED", 10, 30), SYDNEY) is True
    assert within_windows(windows, _at("TUE", 10, 30), SYDNEY) is False


def test_several_windows_compose() -> None:
    windows = parse_windows(("MON-THU,09:00-17:00", "FRI,09:00-12:00"))

    assert within_windows(windows, _at("FRI", 11, 0), SYDNEY) is True
    assert within_windows(windows, _at("FRI", 14, 0), SYDNEY) is False
    assert within_windows(windows, _at("TUE", 14, 0), SYDNEY) is True


def test_no_window_means_no_restriction() -> None:
    assert within_windows((), _at("SUN", 3, 0), SYDNEY) is True


def test_a_wrapping_range_crosses_the_week() -> None:
    windows = parse_windows(("SAT-SUN,00:00-23:59",))

    assert within_windows(windows, _at("SAT", 12, 0), SYDNEY) is True
    assert within_windows(windows, _at("SUN", 12, 0), SYDNEY) is True
    assert within_windows(windows, _at("MON", 12, 0), SYDNEY) is False


def test_a_window_that_crosses_midnight_is_refused() -> None:
    # Two windows say it unambiguously; one that wraps invites a reader to
    # guess whether 23:00-02:00 means three hours or twenty-one.
    with pytest.raises(WindowError, match="midnight"):
        parse_windows(("MON,22:00-02:00",))


@pytest.mark.parametrize(
    "raw",
    ["MONDAY,09:00-17:00", "MON,9-17", "MON09:00-17:00", "MON,09:00", "FRI-MON,09:00-17:00", ""],
)
def test_an_unusable_window_is_refused(raw: str) -> None:
    with pytest.raises(WindowError):
        parse_windows((raw,))


def _config(tmp_path: Path, loop_body: str) -> Path:
    path = tmp_path / "touchstone.toml"
    path.write_text(
        "\n".join(
            [
                "version = 1",
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
                "[engine]",
                'name = "codex"',
                'model = "m"',
                "[loop.tidy]",
                'brief = "builtin:code-audit"',
                'label = "touchstone:tidy"',
                loop_body,
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_a_loop_declares_its_whole_merge_policy(tmp_path: Path) -> None:
    config = load(
        _config(
            tmp_path,
            "\n".join(
                [
                    "auto_merge = true",
                    'auto_merge_strategy = "rebase"',
                    "auto_merge_delete_branch = false",
                    'auto_merge_window = ["MON-THU,09:00-17:00", "FRI,09:00-12:00"]',
                    "auto_merge_max_files = 5",
                ]
            ),
        )
    )
    loop = config.loop("tidy")

    assert loop.auto_merge is True
    assert loop.auto_merge_strategy == "rebase"
    assert loop.auto_merge_delete_branch is False
    assert len(loop.auto_merge_window) == 2
    assert loop.auto_merge_max_files == 5


def test_the_policy_defaults_preserve_what_the_switch_alone_did(tmp_path: Path) -> None:
    loop = load(_config(tmp_path, "auto_merge = true")).loop("tidy")

    assert loop.auto_merge_strategy == "squash"
    assert loop.auto_merge_delete_branch is True
    assert loop.auto_merge_window == ()
    assert loop.auto_merge_max_files == 0


def test_a_policy_without_the_switch_is_refused(tmp_path: Path) -> None:
    # Configuring a strategy and forgetting the switch reads as "this merges
    # by rebase" and behaves as "this never merges". Naming it is the point.
    with pytest.raises(ConfigError, match=r"auto_merge_strategy"):
        load(_config(tmp_path, 'auto_merge_strategy = "rebase"'))


def test_an_unknown_strategy_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"auto_merge_strategy"):
        load(_config(tmp_path, 'auto_merge = true\nauto_merge_strategy = "fast-forward"'))


def test_an_unusable_window_names_the_loop(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"loop\.tidy\.auto_merge_window"):
        load(_config(tmp_path, 'auto_merge = true\nauto_merge_window = ["MONDAY,09:00-17:00"]'))


def test_a_negative_file_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"auto_merge_max_files"):
        load(_config(tmp_path, "auto_merge = true\nauto_merge_max_files = -1"))


def test_the_strategy_reaches_the_forge_command() -> None:
    from touchstone.execution.base import Result
    from touchstone.forge import Forge

    class ExecutorStub:
        def __init__(self) -> None:
            self.argv: list[str] = []

        def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
            self.argv = list(argv)
            return Result(0, "", "")

    for strategy, flag in (("squash", "--squash"), ("merge", "--merge"), ("rebase", "--rebase")):
        executor = ExecutorStub()
        Forge("acme/widgets", executor).arm_auto_merge(  # type: ignore[arg-type]
            7, strategy=strategy, delete_branch=True
        )
        assert flag in executor.argv, f"{strategy} did not reach the command"

    executor = ExecutorStub()
    Forge("acme/widgets", executor).arm_auto_merge(  # type: ignore[arg-type]
        7, strategy="squash", delete_branch=False
    )
    assert "--delete-branch" not in executor.argv


def test_a_strategy_the_repository_forbids_is_refused_before_it_is_attempted() -> None:
    from touchstone.lifecycle import auto_merge_verdict

    def verdict(**overrides: object):  # type: ignore[no-untyped-def]
        conditions: dict[str, object] = {
            "requested": True,
            "independently_verified": True,
            "risk": "low",
            "verdict": "approve",
            "draft": False,
            "gates_passed": True,
            "merge_allowed": True,
            "required_checks_declared": True,
            "protected_branch": True,
            "strategy_allowed": True,
            "within_window": True,
            "files_within_limit": True,
        }
        conditions.update(overrides)
        return auto_merge_verdict(**conditions)  # type: ignore[arg-type]

    assert verdict().armed is True
    # Attempting it would fail after the pull request already existed, and the
    # failure would land somewhere nobody reads.
    assert "strategy" in verdict(strategy_allowed=False).reason
    assert "window" in verdict(within_window=False).reason
    assert "files" in verdict(files_within_limit=False).reason


def test_a_failure_to_arm_is_reported_rather_than_swallowed() -> None:
    from types import SimpleNamespace

    from touchstone.forge import OperationResult
    from touchstone.lifecycle import PublicationRequest, RepositoryLifecycle

    class RefusingForge:
        def repository_info(self) -> dict[str, object]:
            return {"autoMergeAllowed": True, "squashMergeAllowed": True}

        def branch_protection(self, _branch: str) -> bool:
            return True

        def arm_auto_merge(self, _number: int, **_kwargs: object) -> OperationResult:
            return OperationResult(False, "Squash merging is not allowed on this repository")

    request = PublicationRequest(
        finding_id="f",
        loop="code",
        branch="touchstone/a",
        worktree=Path("."),
        base="main",
        label="touchstone:audit",
        escalation_label="touchstone:needs-review",
        risk="low",
        verdict="approve",
        title="T",
        commit_subject="fix: t",
        summary="",
        rationale="",
        review_reason="",
        auto_merge=True,
        independently_verified=True,
        required_checks_declared=True,
    )
    lifecycle = RepositoryLifecycle(
        RefusingForge(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        reap_after_hours=6,
        executor=SimpleNamespace(),  # type: ignore[arg-type]
    )

    outcome = lifecycle._arm_auto_merge(request, 7)

    # Returning "not armed" and discarding why is how a project spends a week
    # wondering which of seven conditions it failed.
    assert outcome.armed is False
    assert "Squash merging is not allowed" in outcome.detail


def test_classify_reports_how_many_files_changed() -> None:

    source = Path("src/touchstone/nodes/classify.py").read_text(encoding="utf-8")

    # Counted where the paths are already known. Recomputing it in publish
    # would reach for `git diff --name-only`, which this module documents at
    # length as the wrong list.
    assert "changed_files" in source
    assert "len(paths)" in source


def test_doctor_names_a_strategy_the_repository_forbids(tmp_path: Path) -> None:
    from touchstone.doctor import merge_policy_checks

    config = load(_config(tmp_path, 'auto_merge = true\nauto_merge_strategy = "rebase"'))

    checks = merge_policy_checks(config, {"rebaseMergeAllowed": False, "autoMergeAllowed": True})

    failed = [check for check in checks if check.level == "FAIL"]
    assert failed, "a forbidden strategy has to be reported before a run needs it"
    assert "rebase" in failed[0].summary
    assert failed[0].repair is not None


def test_doctor_is_quiet_when_the_policy_matches(tmp_path: Path) -> None:
    from touchstone.doctor import merge_policy_checks

    config = load(_config(tmp_path, 'auto_merge = true\nauto_merge_strategy = "rebase"'))

    checks = merge_policy_checks(config, {"rebaseMergeAllowed": True, "autoMergeAllowed": True})

    assert all(check.level == "PASS" for check in checks)


def test_doctor_says_nothing_about_a_loop_that_does_not_auto_merge(tmp_path: Path) -> None:
    from touchstone.doctor import merge_policy_checks

    config = load(_config(tmp_path, "priority = 50"))

    assert merge_policy_checks(config, {"autoMergeAllowed": False}) == []


def test_doctor_reports_a_repository_that_forbids_auto_merge_entirely(tmp_path: Path) -> None:
    from touchstone.doctor import merge_policy_checks

    config = load(_config(tmp_path, "auto_merge = true"))

    checks = merge_policy_checks(config, {"autoMergeAllowed": False, "squashMergeAllowed": True})

    assert [check for check in checks if check.level == "FAIL"]
