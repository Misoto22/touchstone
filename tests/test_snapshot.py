from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from touchstone.hosted.snapshot import compatibility, snapshot_state
from touchstone.outcomes import RunOutcome, RunResult


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    state = tmp_path / "state"
    state.mkdir()
    return SimpleNamespace(
        state_dir=state,
        source=SimpleNamespace(schema_version=2),
        forge=SimpleNamespace(slug="acme/widgets"),
        generated_metadata=SimpleNamespace(source_digest="profile-digest"),
    )


def test_snapshot_selects_only_allowlisted_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.state_dir / "events.jsonl").write_text("events", encoding="utf-8")
    (config.state_dir / "ledger.jsonl").write_text("ledger", encoding="utf-8")
    (config.state_dir / ".env").write_text("SECRET=value", encoding="utf-8")

    plan = snapshot_state(
        config,
        RunResult(RunOutcome.COMPLETED, candidate_id="candidate-1"),
        loop="code",
        run_id="run-1",
        created_at="2026-08-24T12:00:00Z",
    )

    assert set(plan.files) == {"events.jsonl", "ledger.jsonl"}
    assert plan.manifest.lineage == "candidate-1"
    assert ".env" not in plan.files


def test_snapshot_compatibility_returns_typed_clean_start_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    plan = snapshot_state(
        config,
        RunResult(RunOutcome.NO_CHANGE),
        loop="code",
        run_id="run-1",
        created_at="2026-08-24T12:00:00Z",
    )

    assert compatibility(plan.manifest, config, loop="code", lineage=plan.manifest.lineage).ok
    mismatch = compatibility(plan.manifest, config, loop="other", lineage=plan.manifest.lineage)
    assert mismatch.ok is False
    assert mismatch.clean_start_reason == "loop-mismatch"
