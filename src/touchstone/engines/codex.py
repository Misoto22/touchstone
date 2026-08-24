"""Codex, through `codex exec`."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from touchstone.engines.base import Session, engine_environment
from touchstone.execution import Executor


class CodexEngine:
    name = "codex"
    #: `codex exec` reports no cost at all.
    reports_cost = False
    #: Sandbox modes, not per-path rules. `workspace-write` grants the whole
    #: worktree, so the diff check afterwards is the only path enforcement.
    enforces_paths = False

    def __init__(self, config, executor: Executor) -> None:  # type: ignore[no-untyped-def]
        self._config = config
        self._exec = executor

    def _argv(self, *, worktree: str, effort: str, sandbox: str) -> list[str]:
        argv = ["codex", "exec", "-C", worktree]
        if self._config.engine.model:
            argv += ["-m", self._config.engine.model]
        argv += [
            "-c",
            f"model_reasoning_effort={effort}",
            "-s",
            sandbox,
            # The worktree is a linked checkout, not a clone.
            "--skip-git-repo-check",
        ]
        argv += list(self._config.engine.extra_args)
        return argv

    def author(self, brief: str, *, worktree: str, denied: tuple[str, ...]) -> Session:
        # `denied` is accepted and not used, deliberately. Codex has no
        # per-path deny list, and pretending otherwise by filtering the brief
        # would imply an enforcement that does not exist. `enforces_paths` is
        # false; the caller checks the diff.
        argv = self._argv(
            worktree=worktree,
            effort=self._config.engine.audit_effort,
            sandbox="workspace-write",
        )
        argv.append(brief)
        result = self._exec.run(
            argv,
            timeout=self._config.engine.timeout_seconds,
            env=engine_environment(self.name),
        )
        return Session(
            ok=result.ok,
            text=result.stdout,
            cost=None,
            timed_out=result.timed_out,
            detail=result.tail(),
        )

    def review(self, brief: str, *, worktree: str, schema: dict) -> Session:
        schema_path = str(PurePosixPath(worktree) / ".harness-review-schema.json")
        answer_path = str(PurePosixPath(worktree) / ".harness-review-answer.json")
        self._exec.write_text(schema_path, json.dumps(schema))

        argv = self._argv(
            worktree=worktree,
            effort=self._config.engine.review_effort,
            sandbox="read-only",
        )
        argv += ["--output-schema", schema_path, "--output-last-message", answer_path, brief]
        result = self._exec.run(
            argv,
            timeout=self._config.engine.timeout_seconds,
            env=engine_environment(self.name),
        )
        answer = self._exec.read_text(answer_path) or ""
        # Same reason: these are how this engine is asked and answered, not
        # anything the repository should carry.
        self._exec.run(["rm", "-f", schema_path, answer_path], timeout=30)
        return Session(
            ok=result.ok and bool(answer.strip()),
            text=answer,
            cost=None,
            timed_out=result.timed_out,
            detail=result.tail() if not result.ok else "",
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CodexEngine(model={self._config.engine.model!r}, at={self._exec.where!r})"
