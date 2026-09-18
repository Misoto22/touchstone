"""Which Loop a wake signal claims when several are due at once.

A hosted run claims one Due Slot. Ordering equal-priority Loops by loop id
alone handed every wake to whichever name sorted first, so a repository with
`code`, `hardcode` and `naming` on one schedule ran `code` forever and the
other two never once, however long they had been waiting.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from touchstone.outcomes import RunOutcome, RunResult
from touchstone.scheduling.due import DueEvaluator
from touchstone.scheduling.store import DueStore

UTC_NOON = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)


def _config(**priorities: int):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        timezone="UTC",
        loops={
            name: SimpleNamespace(name=name, schedule="hourly@00", priority=priority)
            for name, priority in priorities.items()
        },
    )


def _run(store: DueStore, evaluator: DueEvaluator, config, now: dt.datetime) -> str:  # type: ignore[no-untyped-def]
    """Claim and finish the slot this wake would pick, and name its Loop."""
    selected = evaluator.evaluate(config, now)[0]
    claim = store.claim(selected.slot, owner="worker", now=now, ttl=dt.timedelta(minutes=5))
    store.finish(claim.claim, RunResult(RunOutcome.NO_CHANGE), now=now)  # type: ignore[arg-type]
    return selected.slot.loop_id


def test_equal_priority_rotates_rather_than_starving(tmp_path: Path) -> None:
    store = DueStore(tmp_path / "due.sqlite")
    evaluator = DueEvaluator(store)
    config = _config(code=100, hardcode=100, naming=100)

    picked = [
        _run(store, evaluator, config, UTC_NOON + dt.timedelta(hours=hour)) for hour in range(4)
    ]

    assert set(picked[:3]) == {"code", "hardcode", "naming"}, picked
    assert picked[3] == picked[0], "the rotation should come back around"


def test_priority_still_preempts_a_longer_wait(tmp_path: Path) -> None:
    """Rotation is a tie-break, not a fairness override. A Loop the operator
    put ahead of the others stays ahead even when it has just run and the
    others have been waiting for hours."""
    store = DueStore(tmp_path / "due.sqlite")
    evaluator = DueEvaluator(store)
    config = _config(urgent=10, code=100, naming=100)

    picked = [
        _run(store, evaluator, config, UTC_NOON + dt.timedelta(hours=hour)) for hour in range(3)
    ]

    assert picked == ["urgent", "urgent", "urgent"], picked


def test_the_longest_wait_goes_first_within_one_priority(tmp_path: Path) -> None:
    store = DueStore(tmp_path / "due.sqlite")
    evaluator = DueEvaluator(store)
    config = _config(alpha=100, omega=100)

    _run(store, evaluator, config, UTC_NOON)
    ordered = evaluator.evaluate(config, UTC_NOON + dt.timedelta(hours=1))

    assert ordered[0].slot.loop_id == "omega", "alpha ran, so omega has waited longer"
    assert ordered[0].lateness > ordered[1].lateness
