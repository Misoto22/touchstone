from __future__ import annotations

import json
from pathlib import Path

import pytest

from touchstone.ledger import Ledger, LifecycleEvent, finding_id


def _event(state: str, *, title: str = "Broken invariant") -> LifecycleEvent:
    return LifecycleEvent(
        finding_id=finding_id("code", title),
        state=state,
        title=title,
        loop="code",
        risk="low",
        pr=12,
        head_sha="abc123",
    )


@pytest.mark.parametrize("terminal", ["failed", "reaped", "closed"])
def test_terminal_failure_does_not_suppress_rediscovery(
    tmp_path: Path, terminal: str
) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("armed"))
    ledger.append(_event(terminal))

    assert ledger.suppressed_titles() == []


@pytest.mark.parametrize("state", ["armed", "parked", "merged"])
def test_live_or_completed_publication_suppresses_rediscovery(
    tmp_path: Path, state: str
) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event(state))

    assert "Broken invariant" in ledger.suppressed_titles()[0]


def test_legacy_merging_row_projects_as_armed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps({"status": "merging", "title": "Old finding", "pr": 7}) + "\n",
        encoding="utf-8",
    )

    projection = next(iter(Ledger(path).projections().values()))

    assert projection.state == "armed"
    assert projection.pr == 7


def test_projection_keeps_the_latest_event_for_each_finding(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("proposed"))
    ledger.append(_event("parked"))

    projection = ledger.projection(finding_id("code", "Broken invariant"))

    assert projection is not None
    assert projection.state == "parked"
    assert projection.head_sha == "abc123"
