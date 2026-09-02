from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone import runner
from touchstone.config import load_config
from touchstone.events import EventLog
from touchstone.execution.base import Result
from touchstone.harnesses import HarnessResolutionError
from touchstone.ledger import Ledger, LifecycleEvent


class FetchFailingExecutor:
    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "fetch" in argv:
            return Result(1, "", "authentication failed")
        return Result(0, "", "")


class CleanupFailingExecutor:
    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        return Result(1, "", f"failed: {' '.join(argv[3:5])}")


def test_harness_failure_stops_before_graph_and_persists_reason_code(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_config import _valid_config, _write

    source = _write(
        tmp_path / "touchstone.toml",
        _valid_config() + '\n[harness]\nmode = "embedded"\nentrypoint = "AGENTS.md"\n',
    )
    config = replace(load_config(source), state_dir=tmp_path / "state")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    graph_calls: list[str] = []
    monkeypatch.setattr(runner, "configure", lambda _config: None)
    monkeypatch.setattr(runner, "_gates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_worktree", lambda _config: (str(worktree), "audit/test"))
    monkeypatch.setattr(runner, "_teardown", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        runner,
        "resolve_harness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HarnessResolutionError("harness-entrypoint-missing", "missing AGENTS.md")
        ),
    )
    monkeypatch.setattr(runner, "build", lambda: graph_calls.append("built"))

    assert runner.execute(config, loop="code", dry_run=True) == 3

    assert graph_calls == []
    finished = EventLog(config.state_dir / "events.jsonl").rows()[-1]
    assert finished["reason_code"] == "harness-entrypoint-missing"
    assert finished["detail"] == "missing AGENTS.md"


def test_worktree_creation_refuses_to_use_a_stale_base_after_fetch_failure(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    config = SimpleNamespace(
        execution_repo="/repo",
        execution_worktree="/state/worktree",
        forge=SimpleNamespace(default_branch="main"),
    )
    context = SimpleNamespace(executor=FetchFailingExecutor())
    monkeypatch.setattr(runner, "current", lambda: context)

    with pytest.raises(runner.Held, match="fetch"):
        runner._worktree(config)


def test_run_never_implicitly_reconciles_or_closes_pull_requests() -> None:
    source = inspect.getsource(runner.execute)
    assert ".reconcile(" not in source


def test_teardown_returns_every_cleanup_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = SimpleNamespace(execution_repo="/repo")
    monkeypatch.setattr(
        runner, "current", lambda: SimpleNamespace(executor=CleanupFailingExecutor())
    )

    errors = runner._teardown(config, "/state/worktree", "audit/run", published=False)

    assert len(errors) == 3
    assert all("could not" in error for error in errors)


def test_slot_gate_holds_when_open_pull_state_is_unavailable(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    forge = SimpleNamespace(open_pulls=lambda *_args, **_kwargs: None)
    loop = SimpleNamespace(label="touchstone:audit", drafts_hold_slot=False)
    context = SimpleNamespace(forge=forge, loop=lambda _name: loop)
    config = SimpleNamespace(state_dir=tmp_path, forge=SimpleNamespace())
    monkeypatch.setattr(runner, "current", lambda: context)

    with pytest.raises(runner.Held, match="could not verify the open pull request slot"):
        runner._gates(config, "code", dry_run=False)


def test_partial_remote_write_blocks_new_analysis_until_reconciled(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append(
        LifecycleEvent(
            finding_id="candidate-1",
            state="failed",
            title="Partial publication",
            loop="code",
            branch="touchstone/candidate-1",
            partial=True,
        )
    )
    context = SimpleNamespace(
        ledger=ledger,
        loop=lambda _name: SimpleNamespace(require_change_under=(), label="touchstone:audit"),
    )
    config = SimpleNamespace(state_dir=tmp_path)
    monkeypatch.setattr(runner, "current", lambda: context)

    with pytest.raises(runner.Held, match="partial remote publication"):
        runner._gates(config, "code", dry_run=True)


def _slot_gate(monkeypatch, tmp_path, *, drafts_hold_slot: bool):  # type: ignore[no-untyped-def]
    """Run the slot gate against one open draft carrying the loop's label."""

    draft = {"number": 41, "isDraft": True, "url": "https://example.test/pull/41"}
    asked: dict[str, bool] = {}

    def open_pulls(_label: str, *, include_drafts: bool):  # type: ignore[no-untyped-def]
        asked["include_drafts"] = include_drafts
        return list(draft for _ in (0,)) if include_drafts else []

    loop = SimpleNamespace(label="touchstone:audit", drafts_hold_slot=drafts_hold_slot)
    context = SimpleNamespace(forge=SimpleNamespace(open_pulls=open_pulls), loop=lambda _n: loop)
    monkeypatch.setattr(runner, "current", lambda: context)
    monkeypatch.setattr(runner, "_health_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_publication_gate", lambda *_a, **_k: None)
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")
    config = SimpleNamespace(state_dir=tmp_path, forge=SimpleNamespace())

    runner._gates(config, "code", dry_run=False)
    return asked


def test_a_parked_draft_does_not_hold_the_code_audit_slot(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The code audit parks a medium-risk finding as a draft, and a parked draft
    # waits for a person and is never reaped. A loop whose drafts held the slot
    # would stop at its first medium-risk finding.
    asked = _slot_gate(monkeypatch, tmp_path, drafts_hold_slot=False)

    assert asked["include_drafts"] is False


def test_a_draft_holds_the_slot_for_a_loop_that_allows_one_open_pull(  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path,
) -> None:
    # A harness review is the opposite: one open at a time means open, not
    # open-and-not-a-draft.
    with pytest.raises(runner.Held, match="slot held by #41"):
        _slot_gate(monkeypatch, tmp_path, drafts_hold_slot=True)


def test_source_paths_do_not_decide_the_slot_policy(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The policy was `bool(loop.require_change_under)`, which named the harness
    # review exactly as long as it was the only loop with source paths to
    # maintain. Generated stack evidence sets them for the code audit now.
    from touchstone.config import LoopConfig

    loop = LoopConfig(
        name="code",
        brief="builtin:code-audit",
        label="touchstone:audit",
        config_dir=tmp_path,
        require_change_under=("src/", "tests/"),
    )
    assert loop.drafts_hold_slot is False

    asked: dict[str, bool] = {}

    def open_pulls(_label: str, *, include_drafts: bool):  # type: ignore[no-untyped-def]
        asked["include_drafts"] = include_drafts
        return []

    context = SimpleNamespace(forge=SimpleNamespace(open_pulls=open_pulls), loop=lambda _n: loop)
    monkeypatch.setattr(runner, "current", lambda: context)
    monkeypatch.setattr(runner, "_health_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_publication_gate", lambda *_a, **_k: None)
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")

    config = SimpleNamespace(state_dir=tmp_path, forge=SimpleNamespace())
    runner._gates(config, "code", dry_run=False)

    assert asked["include_drafts"] is False
