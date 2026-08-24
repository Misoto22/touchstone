from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from touchstone.outcomes import ChangeState, RunOutcome, RunResult
from touchstone.runner import run_due

NOW = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        state_dir=tmp_path / "state",
        timezone="UTC",
        loops={
            "a": SimpleNamespace(name="a", schedule="hourly@00", priority=10),
            "b": SimpleNamespace(name="b", schedule="hourly@00", priority=20),
            "c": SimpleNamespace(name="c", schedule="hourly@00", priority=30),
        },
    )


def test_no_change_continues_but_active_change_stops_remaining_due_loops(
    tmp_path: Path,
) -> None:
    outcomes = {
        "a": RunResult(RunOutcome.NO_CHANGE),
        "b": RunResult(RunOutcome.COMPLETED, lifecycle=ChangeState.AWAITING_CHECKS),
        "c": RunResult(RunOutcome.NO_CHANGE),
    }

    report = run_due(
        _config(tmp_path),
        now=NOW,
        loop=None,
        force=False,
        execute_loop=lambda _config, name: outcomes[name],
    )

    assert report.started == ("a", "b")
    assert report.remaining_due == ("c",)
    assert [result.outcome for result in report.results] == [
        RunOutcome.NO_CHANGE,
        RunOutcome.COMPLETED,
    ]


def test_repository_block_stops_remaining_loops(tmp_path: Path) -> None:
    report = run_due(
        _config(tmp_path),
        now=NOW,
        loop=None,
        force=False,
        execute_loop=lambda _config, _name: RunResult(RunOutcome.BLOCKED),
    )

    assert report.started == ("a",)
    assert report.remaining_due == ("b", "c")
    assert report.exit_code == 3


def test_force_requires_one_loop_and_still_calls_the_normal_runner(tmp_path: Path) -> None:
    calls: list[str] = []

    report = run_due(
        _config(tmp_path),
        now=NOW,
        loop="c",
        force=True,
        execute_loop=lambda _config, name: calls.append(name) or RunResult(RunOutcome.REHEARSED),
    )

    assert calls == ["c"]
    assert report.started == ("c",)
    assert report.results[0].outcome == RunOutcome.REHEARSED
