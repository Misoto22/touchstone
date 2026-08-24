from __future__ import annotations

import datetime as dt
from pathlib import Path

from touchstone.config import LoopConfig
from touchstone.forge import OperationResult, PullState
from touchstone.ledger import Ledger, LifecycleEvent, finding_id
from touchstone.lifecycle import RepositoryLifecycle

NOW = dt.datetime(2026, 8, 24, 12, 0, tzinfo=dt.UTC)


class MemoryForge:
    def __init__(self) -> None:
        self.pulls: dict[int, PullState] = {}
        self.closed: list[int] = []

    def pull(self, number: int) -> PullState | None:
        return self.pulls.get(number)

    def close(self, number: int, comment: str) -> OperationResult:
        self.closed.append(number)
        return OperationResult(True)


def _loop() -> LoopConfig:
    return LoopConfig(
        name="code",
        brief="builtin:code-audit",
        label="touchstone:audit",
        config_dir=Path.cwd(),
    )


def _seed(ledger: Ledger, state: str, *, pr: int = 12) -> str:
    identifier = finding_id("code", "Broken invariant")
    ledger.append(
        LifecycleEvent(
            finding_id=identifier,
            state=state,
            title="Broken invariant",
            loop="code",
            risk="low",
            pr=pr,
            head_sha="abc123",
        )
    )
    return identifier


def _pull(
    *,
    number: int = 12,
    draft: bool = False,
    check_state: str = "success",
    age_hours: int = 1,
    merged_at: str | None = None,
    closed: bool = False,
) -> PullState:
    created = NOW - dt.timedelta(hours=age_hours)
    return PullState(
        number=number,
        head_sha="abc123",
        branch="touchstone/run-1",
        draft=draft,
        check_state=check_state,
        merged_at=merged_at,
        closed=closed,
        created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        url=f"https://github.com/acme/widgets/pull/{number}",
    )


def test_reconcile_records_merged_when_github_has_merged_pull(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    identifier = _seed(ledger, "armed")
    forge = MemoryForge()
    forge.pulls[12] = _pull(merged_at="2026-08-24T11:00:00Z")

    report = RepositoryLifecycle(forge, ledger, reap_after_hours=6).reconcile(_loop(), NOW)

    assert report.merged == (12,)
    assert ledger.projection(identifier).state == "merged"  # type: ignore[union-attr]


def test_reaper_closes_only_old_failed_non_drafts(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    identifier = _seed(ledger, "armed")
    forge = MemoryForge()
    forge.pulls[12] = _pull(age_hours=7, check_state="failure", draft=False)

    report = RepositoryLifecycle(forge, ledger, reap_after_hours=6).reconcile(_loop(), NOW)

    assert report.reaped == (12,)
    assert forge.closed == [12]
    assert ledger.projection(identifier).state == "reaped"  # type: ignore[union-attr]


def test_reaper_never_closes_parked_drafts(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    identifier = _seed(ledger, "parked")
    forge = MemoryForge()
    forge.pulls[12] = _pull(age_hours=24 * 90, check_state="failure", draft=True)

    report = RepositoryLifecycle(forge, ledger, reap_after_hours=6).reconcile(_loop(), NOW)

    assert report.reaped == ()
    assert forge.closed == []
    assert ledger.projection(identifier).state == "awaiting_human"  # type: ignore[union-attr]


def test_github_lookup_failure_is_inconclusive_and_mutates_nothing(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    identifier = _seed(ledger, "armed")

    report = RepositoryLifecycle(MemoryForge(), ledger, reap_after_hours=6).reconcile(_loop(), NOW)

    assert report.inconclusive == (12,)
    assert ledger.projection(identifier).state == "awaiting_checks"  # type: ignore[union-attr]
