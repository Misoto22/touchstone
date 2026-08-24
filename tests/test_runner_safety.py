from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from touchstone import runner
from touchstone.execution.base import Result


class FetchFailingExecutor:
    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "fetch" in argv:
            return Result(1, "", "authentication failed")
        return Result(0, "", "")


class CleanupFailingExecutor:
    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        return Result(1, "", f"failed: {' '.join(argv[3:5])}")


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
    loop = SimpleNamespace(label="touchstone:audit", require_change_under=())
    context = SimpleNamespace(forge=forge, loop=lambda _name: loop)
    config = SimpleNamespace(state_dir=tmp_path, forge=SimpleNamespace())
    monkeypatch.setattr(runner, "current", lambda: context)

    with pytest.raises(runner.Held, match="could not verify the open pull request slot"):
        runner._gates(config, "code", dry_run=False)
