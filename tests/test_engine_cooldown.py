"""An engine the provider has refused stays paused until it said it would recover."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone import cooldown, runner
from touchstone.config import EngineConfig
from touchstone.engines.base import Session
from touchstone.engines.limits import UsageLimit
from touchstone.hosted.snapshot import snapshot_state
from touchstone.nodes import audit
from touchstone.outcomes import RunOutcome, RunResult

NOW = dt.datetime(2026, 9, 17, 22, 5, tzinfo=dt.UTC)
RESET = dt.datetime(2026, 9, 19, 20, 59, tzinfo=dt.UTC)
CODEX = EngineConfig(name="codex")


def test_a_recorded_pause_holds_until_its_reset_and_no_longer(tmp_path: Path) -> None:
    cooldown.record(tmp_path, CODEX, until=RESET, reason="usage limit was reached")

    paused = cooldown.active(tmp_path, CODEX, now=NOW)

    assert paused is not None
    assert paused.until == RESET
    assert paused.reason == "usage limit was reached"
    assert cooldown.active(tmp_path, CODEX, now=RESET) is None


def test_a_pause_belongs_to_one_pool_member(tmp_path: Path) -> None:
    """Two Loops on one member share a plan; a member on another key does not."""

    cooldown.record(tmp_path, CODEX, until=RESET, reason="usage limit was reached")

    assert cooldown.active(tmp_path, replace(CODEX, model="other-model"), now=NOW) is not None
    assert cooldown.active(tmp_path, replace(CODEX, api_key_env="TEAM_KEY"), now=NOW) is None
    assert cooldown.active(tmp_path, EngineConfig(name="claude"), now=NOW) is None


def test_an_unreadable_pause_refuses_and_says_how_to_clear_it(tmp_path: Path) -> None:
    (tmp_path / cooldown.COOLDOWN_FILE).write_text("{not json", encoding="utf-8")

    paused = cooldown.active(tmp_path, CODEX, now=NOW)

    assert paused is not None
    assert cooldown.COOLDOWN_FILE in paused.reason


def test_no_pause_file_means_no_pause(tmp_path: Path) -> None:
    assert cooldown.active(tmp_path, CODEX, now=NOW) is None


def test_the_gate_refuses_before_the_forge_is_even_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cooldown.record(
        tmp_path,
        CODEX,
        until=dt.datetime.now(dt.UTC) + dt.timedelta(hours=2),
        reason="usage limit was reached",
    )
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")

    def open_pulls(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the slot was checked while the engine was paused")

    context = SimpleNamespace(
        forge=SimpleNamespace(open_pulls=open_pulls),
        loop=lambda _name: SimpleNamespace(label="touchstone:audit", drafts_hold_slot=False),
    )
    monkeypatch.setattr(runner, "current", lambda: context)
    config = SimpleNamespace(
        state_dir=tmp_path, forge=SimpleNamespace(), engine_for=lambda _l: CODEX
    )

    with pytest.raises(runner.Held, match="codex paused until") as held:
        runner._gates(config, "code", dry_run=False)

    assert held.value.reason_code == "engine-cooldown"


def test_a_rehearsal_is_refused_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A rehearsal buys an author session like any other run."""

    cooldown.record(
        tmp_path,
        CODEX,
        until=dt.datetime.now(dt.UTC) + dt.timedelta(hours=2),
        reason="usage limit was reached",
    )
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(runner, "current", lambda: SimpleNamespace(loop=lambda _name: None))
    config = SimpleNamespace(state_dir=tmp_path, engine_for=lambda _l: CODEX)

    with pytest.raises(runner.Held, match="paused until"):
        runner._gates(config, "code", dry_run=True)


class _LimitedEngine:
    name = "codex"

    def author(self, *_args, **_kwargs) -> Session:  # type: ignore[no-untyped-def]
        return Session(
            ok=False,
            text="",
            cost=None,
            detail="ERROR: You've hit your usage limit. Visit https://example.test",
            limited=UsageLimit("usage limit was reached", RESET),
        )


def _audit_context(tmp_path: Path, engine) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    loop = SimpleNamespace(
        name="code", prompt=lambda: "brief", protected_paths=(), model="", attachment=()
    )
    return SimpleNamespace(
        config=SimpleNamespace(state_dir=tmp_path, engine_for=lambda _l: CODEX),
        loop=lambda _name: loop,
        harness_prompt=lambda: "",
        ledger=SimpleNamespace(handled_titles=lambda: []),
        engine_for=lambda _name: engine,
    )


def test_a_limited_audit_pauses_the_engine_and_says_until_when(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(audit, "current", lambda: _audit_context(tmp_path, _LimitedEngine()))

    update = audit.run({"loop": "code", "worktree": str(tmp_path)})

    assert update["outcome"] == "held"
    assert update["notes"] == [
        f"the codex usage limit was reached; paused until {RESET.isoformat()}"
    ]
    assert "example.test" not in " ".join(update["notes"]), "transcript leaked into a note"
    paused = cooldown.active(tmp_path, CODEX, now=NOW)
    assert paused is not None and paused.until == RESET


def test_an_ordinary_failure_pauses_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Failing:
        name = "codex"

        def author(self, *_args, **_kwargs) -> Session:  # type: ignore[no-untyped-def]
            return Session(ok=False, text="", cost=None, timed_out=True)

    monkeypatch.setattr(audit, "current", lambda: _audit_context(tmp_path, _Failing()))

    update = audit.run({"loop": "code", "worktree": str(tmp_path)})

    assert update["notes"] == ["the codex session timed out"]
    assert not (tmp_path / cooldown.COOLDOWN_FILE).exists()


def test_the_pause_travels_in_the_hosted_state_snapshot(tmp_path: Path) -> None:
    """Hosted runs start from the snapshot; a pause left out of it is forgotten."""

    cooldown.record(tmp_path, CODEX, until=RESET, reason="usage limit was reached")
    config = SimpleNamespace(
        state_dir=tmp_path,
        forge=SimpleNamespace(slug="acme/widgets"),
        source=SimpleNamespace(schema_version=2),
        generated_metadata=None,
    )

    plan = snapshot_state(
        config, RunResult(RunOutcome.BLOCKED), loop="__repository__", run_id="1", created_at="t"
    )

    assert cooldown.COOLDOWN_FILE in plan.files
