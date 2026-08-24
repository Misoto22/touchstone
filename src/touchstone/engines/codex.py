"""Codex, through `codex exec`."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from touchstone.engines.base import Session, blocked_reason, engine_environment, keep
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

    def _argv(self, *, worktree: str, effort: str, sandbox: str, model: str = "") -> list[str]:
        argv = ["codex", "exec", "-C", worktree]
        chosen = model or self._config.engine.model
        if chosen:
            argv += ["-m", chosen]
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

    def _session(self, result, transcript: str, *, cost: float | None = None) -> Session:  # type: ignore[no-untyped-def]
        blocked = blocked_reason(transcript)
        return Session(
            # A blocked session is not ok, whatever the exit code said. This is
            # the whole point: `codex exec` exits 0 after refusing every write.
            ok=result.ok and blocked is None,
            text=result.stdout,
            cost=cost,
            timed_out=result.timed_out,
            detail=blocked or result.tail(),
            blocked=blocked,
        )

    def _model_environment(self) -> dict[str, str] | None:
        """The environment for a model call, when this executor can replace one.

        Over ssh it cannot: assignments would be appended to the remote command,
        overriding the configured remote `PATH` and `HOME` and putting a local
        API key on a remote command line. The remote environment is configured
        by `execution.ssh.env`, which is where an ssh installation's credentials
        already belong.
        """
        if not self._exec.replaces_environment:
            return None
        return engine_environment(self.name)

    def author(
        self, brief: str, *, worktree: str, denied: tuple[str, ...], model: str = ""
    ) -> Session:
        # `denied` is accepted and not used, deliberately. Codex has no
        # per-path deny list, and pretending otherwise by filtering the brief
        # would imply an enforcement that does not exist. `enforces_paths` is
        # false; the caller checks the diff.
        argv = self._argv(
            worktree=worktree,
            effort=self._config.engine.audit_effort,
            model=model,
            sandbox=self._config.engine.sandbox,
        )
        argv.append(brief)
        result = self._exec.run(
            argv,
            timeout=self._config.engine.timeout_seconds,
            env=self._model_environment(),
        )
        transcript = result.stdout + result.stderr
        keep(self._config.state_dir, "engine-author.log", transcript)
        return self._session(result, transcript)

    def review(self, brief: str, *, worktree: str, schema: dict, model: str = "") -> Session:
        schema_path = str(PurePosixPath(worktree) / ".harness-review-schema.json")
        answer_path = str(PurePosixPath(worktree) / ".harness-review-answer.json")
        self._exec.write_text(schema_path, json.dumps(schema))

        argv = self._argv(
            worktree=worktree,
            effort=self._config.engine.review_effort,
            model=model,
            sandbox="read-only",
        )
        argv += ["--output-schema", schema_path, "--output-last-message", answer_path, brief]
        result = self._exec.run(
            argv,
            timeout=self._config.engine.timeout_seconds,
            env=self._model_environment(),
        )
        answer = self._exec.read_text(answer_path) or ""
        transcript = result.stdout + result.stderr
        keep(self._config.state_dir, "engine-review.log", transcript)
        # These two are how this engine is asked and answered, not anything
        # the repository should carry.
        self._exec.run(["rm", "-f", schema_path, answer_path], timeout=30)
        session = self._session(result, transcript)
        return Session(
            ok=session.ok and bool(answer.strip()),
            text=answer,
            cost=None,
            timed_out=session.timed_out,
            detail=session.detail,
            blocked=session.blocked,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CodexEngine(model={self._config.engine.model!r}, at={self._exec.where!r})"
