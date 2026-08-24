from __future__ import annotations

from types import SimpleNamespace

import pytest

from touchstone import runner
from touchstone.execution.base import Result


class FetchFailingExecutor:
    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "fetch" in argv:
            return Result(1, "", "authentication failed")
        return Result(0, "", "")


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
