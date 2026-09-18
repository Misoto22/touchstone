"""Hosted Analysis asks the local runner's questions before buying a session.

The hosted probe repository reached 91 open Touchstone pull requests: Analysis
held no GitHub token, never asked whether the Loop's slot was held or the
default branch healthy, and bought an author session on every wake.
"""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.hosted_gates import open_gates, with_engine
from touchstone import cooldown
from touchstone.config import EngineConfig
from touchstone.hosted import gates
from touchstone.hosted.gates import ForgeGates, Holder
from touchstone.hosted.runtime import run_stage
from touchstone.hosted.workflow import ActionPins, render_workflow
from touchstone.scheduling.store import DueStore

NOW = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)


class _Forge:
    def __init__(self, *, pulls=None, conclusion: str = "success") -> None:  # type: ignore[no-untyped-def]
        self.pulls = pulls if pulls is not None else {}
        self.conclusion = conclusion
        self.asked: list[tuple[str, bool]] = []

    def open_pulls(self, label: str, *, include_drafts: bool):  # type: ignore[no-untyped-def]
        self.asked.append((label, include_drafts))
        return self.pulls.get(label, [])

    def latest_run(self, _workflow: str, *, branch: str) -> str:
        return self.conclusion


def _loops() -> dict[str, SimpleNamespace]:
    return {
        "code": SimpleNamespace(
            name="code",
            label="touchstone:audit",
            drafts_hold_slot=False,
            schedule="hourly@00",
            priority=10,
            targets=(),
        ),
        "harness": SimpleNamespace(
            name="harness",
            label="touchstone:harness",
            drafts_hold_slot=True,
            schedule="hourly@00",
            priority=20,
            targets=(),
        ),
    }


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = SimpleNamespace(
        repo_path=tmp_path,
        state_dir=tmp_path / ".touchstone" / "state",
        source=SimpleNamespace(schema_version=2),
        forge=SimpleNamespace(
            slug="acme/widgets", default_branch="main", required_workflows=("ci.yml",)
        ),
        execution=SimpleNamespace(target="local"),
        loops=_loops(),
        timezone="UTC",
        generated_metadata=SimpleNamespace(source_digest="profile-digest"),
    )
    config.loop = lambda name: config.loops[name]
    return with_engine(config)


def test_collect_asks_each_loop_with_its_own_draft_policy(tmp_path: Path) -> None:
    forge = _Forge(pulls={"touchstone:audit": [{"number": 7, "url": "https://x.test/7"}]})

    facts = gates.collect(_config(tmp_path), forge)

    assert sorted(forge.asked) == [("touchstone:audit", False), ("touchstone:harness", True)]
    assert facts.health == ""
    assert facts.slots["code"] == (Holder(7, "https://x.test/7"),)
    assert facts.slots["harness"] == ()


def test_collect_records_an_unhealthy_default_branch(tmp_path: Path) -> None:
    facts = gates.collect(_config(tmp_path), _Forge(conclusion="failure"))

    assert facts.health == "production not known good: ci.yml=failure"
    assert facts.refusal("code") == ("production-not-known-good", facts.health)


def test_an_unanswerable_slot_refuses_rather_than_passes(tmp_path: Path) -> None:
    forge = _Forge()
    forge.open_pulls = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    facts = gates.collect(_config(tmp_path), forge)

    assert facts.refusal("code") == (
        "slot-unverifiable",
        "could not verify the open pull request slot",
    )
    assert facts.refusal("unknown-loop") is not None


def test_a_held_slot_is_reported_before_health(tmp_path: Path) -> None:
    facts = ForgeGates("production not known good: ci.yml=failure", {"code": (Holder(9, "u"),)})

    assert facts.refusal("code") == ("slot-held", "slot held by #9: u")


def test_a_reanalysis_is_not_blocked_by_the_draft_it_replaces() -> None:
    facts = ForgeGates("", {"harness": (Holder(12, "u"),)})

    assert facts.refusal("harness", excluding=12) is None
    assert facts.refusal("harness", excluding=13) == ("slot-held", "slot held by #12: u")


def test_facts_survive_the_artifact_round_trip(tmp_path: Path) -> None:
    written = ForgeGates("", {"code": (Holder(3, "https://x.test/3"),), "harness": None})
    path = tmp_path / gates.GATES_FILE

    written.write(path)

    assert gates.read(path) == written


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"version": 2, "health": "", "slots": {}}',
        '{"version": 1, "health": "", "slots": {"code": [{"number": 0, "url": ""}]}}',
        '{"version": 1, "health": "", "slots": {"code": [{"number": true, "url": ""}]}}',
        '{"version": 1, "health": 3, "slots": {}}',
    ],
)
def test_malformed_facts_are_refused(tmp_path: Path, payload: str) -> None:
    path = tmp_path / gates.GATES_FILE
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError):
        gates.read(path)


def _env() -> dict[str, str]:
    return {
        "TOUCHSTONE_STATE_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode(),
        "TOUCHSTONE_NOW": NOW.isoformat(),
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
    }


def _no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("an author session was bought")

    monkeypatch.setattr("touchstone.hosted.runtime._ensure_engine", refuse)
    monkeypatch.setattr("touchstone.hosted.runtime._analyze_loop", refuse)


def test_analysis_buys_no_session_while_the_slot_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.loops = {"code": config.loops["code"]}
    open_gates(config, slots={"code": (Holder(84, "https://x.test/84"),)})
    _no_session(monkeypatch)

    analysis = run_stage(config, "analysis", env=_env())
    run_stage(config, "snapshot", env=_env())

    assert analysis.outcome == "blocked"
    assert analysis.reason_code == "slot-held"
    record = DueStore(Path(config.state_dir) / "due.sqlite").records()[0]
    assert record.consumed_at == NOW, "a blocked wake must consume its slot, not retry it"


def test_analysis_refuses_when_prepare_recorded_no_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.loops = {"code": config.loops["code"]}
    _no_session(monkeypatch)

    analysis = run_stage(config, "analysis", env=_env())

    assert analysis.outcome == "blocked"
    assert analysis.reason_code == "forge-gates-unavailable"


def test_analysis_buys_no_session_while_the_engine_is_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.loops = {"code": config.loops["code"]}
    open_gates(config)
    cooldown.record(
        config.state_dir,
        EngineConfig(name="codex"),
        until=NOW + dt.timedelta(days=1),
        reason="usage limit was reached",
    )
    _no_session(monkeypatch)
    # The pause arrives in the restored snapshot, exactly as a previous run left it.
    from touchstone.hosted import runtime
    from touchstone.outcomes import RunOutcome, RunResult

    prepared = tmp_path / ".touchstone" / "hosted" / "prepare"
    runtime._write_state_bundle(
        config,
        prepared / "state.bundle.json",
        key=bytes(range(32)),
        run_id="prior",
        result=RunResult(RunOutcome.BLOCKED),
    )
    (Path(config.state_dir) / cooldown.COOLDOWN_FILE).unlink()

    analysis = run_stage(config, "analysis", env=_env())

    assert analysis.outcome == "blocked"
    assert analysis.reason_code == "engine-cooldown"


def test_prepare_records_the_gates_with_its_read_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from touchstone.hosted import runtime
    from touchstone.outcomes import RunOutcome, RunResult

    config = _config(tmp_path)
    previous = tmp_path / "previous" / "state.bundle.json"
    runtime._write_state_bundle(
        config,
        previous,
        key=bytes(range(32)),
        run_id="prior",
        result=RunResult(RunOutcome.NO_CHANGE),
    )
    forge = _Forge(pulls={"touchstone:harness": [{"number": 5, "url": "https://x.test/5"}]})
    monkeypatch.setattr("touchstone.forge.Forge", lambda *_args, **_kwargs: forge)

    output = run_stage(
        config,
        "prepare",
        env={
            "GH_TOKEN": "read-only",
            "TOUCHSTONE_PREVIOUS_STATE_BUNDLE": str(previous),
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
        },
    )

    facts = gates.read(tmp_path / ".touchstone" / "hosted" / "prepare" / gates.GATES_FILE)
    assert output.outcome == "completed"
    assert facts.refusal("harness") == ("slot-held", "slot held by #5: https://x.test/5")
    assert facts.refusal("code") is None


def test_the_prepare_job_may_read_pull_requests_and_nothing_more(tmp_path: Path) -> None:
    config = SimpleNamespace(
        repo_path=tmp_path,
        source=SimpleNamespace(schema_version=2),
        forge=SimpleNamespace(default_branch="main", slug="acme/widgets"),
        engine=SimpleNamespace(name="codex", key_env="OPENAI_API_KEY"),
        engines={},
        execution=SimpleNamespace(target="local", ssh=None),
        git=SimpleNamespace(),
        timezone="UTC",
        loops={},
        targets={},
        generated_metadata=None,
        actions=SimpleNamespace(
            visibility="private",
            wake_minutes=60,
            artifact_retention_days=90,
            node_version="24",
            action_sha="",
            approval_environment="",
            auto_merge=False,
        ),
    )
    text = render_workflow(config, ActionPins(), action_sha="a" * 40)
    prepare, rest = text.split("  analysis:", 1)

    assert "pull-requests: read" in prepare
    assert "pull-requests: write" not in text
    assert "pull-requests" not in rest, "only Prepare needs to list pull requests"
