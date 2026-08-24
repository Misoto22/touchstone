from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from touchstone.outcomes import RunOutcome, RunResult
from touchstone.scheduling.due import DueEvaluator, DueSlot, schedule_generation
from touchstone.scheduling.store import DueStore

UTC_NOON = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)


def _slot(*, generation: str = "gen-1") -> DueSlot:
    return DueSlot("code", generation, UTC_NOON)


def test_due_slot_identity_includes_schedule_generation() -> None:
    first = DueSlot(
        "code",
        schedule_generation("code", "hourly@00", "UTC"),
        UTC_NOON,
    )
    changed = DueSlot(
        "code",
        schedule_generation("code", "hourly@30", "UTC"),
        UTC_NOON,
    )

    assert first.id != changed.id


def test_expired_claim_can_be_reacquired(tmp_path: Path) -> None:
    store = DueStore(tmp_path / "due.sqlite")
    slot = _slot()

    first = store.claim(slot, owner="one", now=UTC_NOON, ttl=dt.timedelta(minutes=5))
    second = store.claim(
        slot,
        owner="two",
        now=UTC_NOON + dt.timedelta(minutes=6),
        ttl=dt.timedelta(minutes=5),
    )

    assert first.acquired and second.acquired
    assert second.claim.owner == "two"  # type: ignore[union-attr]


def test_repository_allows_only_one_active_mutating_claim(tmp_path: Path) -> None:
    store = DueStore(tmp_path / "due.sqlite")
    first = store.claim(_slot(), owner="one", now=UTC_NOON, ttl=dt.timedelta(minutes=5))
    other = store.claim(
        DueSlot("other", "gen", UTC_NOON),
        owner="two",
        now=UTC_NOON,
        ttl=dt.timedelta(minutes=5),
    )

    assert first.acquired is True
    assert other.acquired is False
    assert other.reason == "repository-claimed"


def test_completed_and_blocked_slots_are_consumed_but_failures_retry(tmp_path: Path) -> None:
    store = DueStore(tmp_path / "due.sqlite")
    for index, outcome in enumerate((RunOutcome.COMPLETED, RunOutcome.BLOCKED)):
        slot = DueSlot(f"loop-{index}", "gen", UTC_NOON)
        claimed = store.claim(slot, owner="worker", now=UTC_NOON, ttl=dt.timedelta(minutes=5))
        store.finish(claimed.claim, RunResult(outcome), now=UTC_NOON)  # type: ignore[arg-type]
        assert not store.claim(
            slot,
            owner="again",
            now=UTC_NOON + dt.timedelta(hours=1),
            ttl=dt.timedelta(minutes=5),
        ).acquired

    failed = _slot()
    for attempt in range(3):
        now = UTC_NOON + dt.timedelta(hours=attempt)
        claim = store.claim(failed, owner=f"worker-{attempt}", now=now, ttl=dt.timedelta(minutes=5))
        assert claim.acquired
        store.finish(
            claim.claim,  # type: ignore[arg-type]
            RunResult(RunOutcome.FAILED, retryable=True),
            now=now,
        )
    assert store.record(failed.id).consumed_at is not None  # type: ignore[union-attr]
    assert store.record(failed.id).attempts == 3  # type: ignore[union-attr]


def test_evaluator_clean_start_and_coalesces_missed_periods(tmp_path: Path) -> None:
    store = DueStore(tmp_path / "due.sqlite")
    config = SimpleNamespace(
        timezone="UTC",
        loops={
            "code": SimpleNamespace(name="code", schedule="hourly@00", priority=10),
            "manual": SimpleNamespace(name="manual", schedule=None, priority=10),
        },
    )
    evaluator = DueEvaluator(store)

    first = evaluator.evaluate(config, UTC_NOON)
    assert len(first) == 1
    assert first[0].clean_start is True
    claim = store.claim(first[0].slot, owner="worker", now=UTC_NOON, ttl=dt.timedelta(minutes=5))
    store.finish(
        claim.claim,
        RunResult(RunOutcome.NO_CHANGE),
        now=UTC_NOON,  # type: ignore[arg-type]
    )

    catch_up = evaluator.evaluate(config, UTC_NOON + dt.timedelta(hours=3, minutes=10))
    assert catch_up[0].slot.scheduled_for_utc == UTC_NOON + dt.timedelta(hours=3)
    assert catch_up[0].missed_count == 3
    assert catch_up[0].lateness == dt.timedelta(minutes=10)
