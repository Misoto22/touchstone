from __future__ import annotations

import json
from pathlib import Path

import pytest

from touchstone.ledger import Ledger, LifecycleEvent, candidate_id, finding_id


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
def test_terminal_failure_does_not_suppress_rediscovery(tmp_path: Path, terminal: str) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("armed"))
    ledger.append(_event(terminal))

    assert ledger.suppressed_titles() == []


@pytest.mark.parametrize("state", ["armed", "parked", "merged"])
def test_live_or_completed_publication_suppresses_rediscovery(tmp_path: Path, state: str) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event(state))

    assert "Broken invariant" in ledger.suppressed_titles()[0]


def test_legacy_merging_row_projects_as_awaiting_checks(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps({"status": "merging", "title": "Old finding", "pr": 7}) + "\n",
        encoding="utf-8",
    )

    projection = next(iter(Ledger(path).projections().values()))

    assert projection.state == "awaiting_checks"
    assert projection.pr == 7


def test_projection_keeps_the_latest_event_for_each_finding(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(_event("proposed"))
    ledger.append(_event("parked"))

    projection = ledger.projection(finding_id("code", "Broken invariant"))

    assert projection is not None
    assert projection.state == "awaiting_human"
    assert projection.head_sha == "abc123"


def test_candidate_identity_binds_finding_base_patch_and_run() -> None:
    stable = finding_id("code", "Broken invariant")
    first = candidate_id(stable, "a" * 40, "sha256:" + "b" * 64, "run-1")

    assert first == candidate_id(stable, "a" * 40, "sha256:" + "b" * 64, "run-1")
    assert first != candidate_id(stable, "a" * 40, "sha256:" + "c" * 64, "run-1")
    assert first != candidate_id(stable, "a" * 40, "sha256:" + "b" * 64, "run-2")


def test_an_open_row_round_trips_the_files_it_edits(tmp_path: Path) -> None:
    """Written by `append` via `asdict`, read back by `projections`. A tuple that
    survives as a JSON array and returns as a tuple is the whole contract; a
    silent shape change here is a session steered around nothing."""
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(
        LifecycleEvent(
            finding_id=finding_id("code", "Forked default"),
            state="armed",
            title="Forked default",
            loop="code",
            paths=("src/retry.py", "tests/test_retry.py"),
        )
    )

    row = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").strip())
    assert row["paths"] == ["src/retry.py", "tests/test_retry.py"]

    projection = ledger.projections()[finding_id("code", "Forked default")]
    assert projection.paths == ("src/retry.py", "tests/test_retry.py")
