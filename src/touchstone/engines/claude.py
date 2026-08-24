"""Claude Code, through `claude -p`."""

from __future__ import annotations

import json

from touchstone.engines.base import Session, blocked_reason, engine_environment, keep
from touchstone.execution import Executor


class ClaudeEngine:
    name = "claude"
    #: Reports `total_cost_usd`, and `--max-budget-usd` enforces a ceiling.
    reports_cost = True
    #: Takes a per-path deny list, so the harness is unreachable at the
    #: permission layer rather than only checked afterwards.
    enforces_paths = True

    def __init__(self, config, executor: Executor) -> None:  # type: ignore[no-untyped-def]
        self._config = config
        self._exec = executor

    def _settings(self, denied: tuple[str, ...]) -> str:
        """A settings file denying every protected path, written where the session runs."""
        rules: list[str] = []
        for path in denied:
            pattern = f"./{path.rstrip('/')}/**" if path.endswith("/") else f"./{path}"
            rules += [f"Edit({pattern})", f"Write({pattern})"]
        # Publishing belongs to the loop, which is the only thing that knows the
        # risk class. A session able to push would route around its own review.
        rules += [
            "Bash(git push:*)",
            "Bash(git commit:*)",
            "Bash(gh pr:*)",
            "Bash(gh api:*)",
            "Bash(op:*)",
            "Read(./.env)",
            "Read(./.env.*)",
        ]
        return json.dumps({"permissions": {"deny": rules}})

    def author(self, brief: str, *, worktree: str, denied: tuple[str, ...]) -> Session:
        settings_path = f"{worktree}/.harness-settings.json"
        self._exec.write_text(settings_path, self._settings(denied))

        argv = [
            "claude",
            "-p",
            "--model",
            self._config.engine.model,
            "--effort",
            self._config.engine.audit_effort,
            "--max-budget-usd",
            str(self._config.engine.budget.audit),
            # On the audit and deliberately not on the review: an audit that
            # degrades still produces a finding a person can read, while a
            # review that degrades has quietly changed who approves an
            # unattended production merge.
            "--fallback-model",
            "sonnet",
            "--permission-mode",
            "acceptEdits",
            "--settings",
            settings_path,
            "--output-format",
            "json",
            *self._config.engine.extra_args,
            brief,
        ]
        result = self._exec.run(
            argv,
            cwd=worktree,
            timeout=self._config.engine.timeout_seconds,
            env=engine_environment(self.name),
        )
        self._exec.run(["rm", "-f", settings_path], timeout=30)
        transcript = result.stdout + result.stderr
        keep(self._config.state_dir, "engine-author.log", transcript)
        blocked = blocked_reason(transcript)
        cost, _ = _payload(result.stdout)
        return Session(
            blocked=blocked,
            ok=result.ok and blocked is None,
            text=result.stdout,
            cost=cost,
            timed_out=result.timed_out,
            detail=blocked or result.tail(),
        )

    def review(self, brief: str, *, worktree: str, schema: dict) -> Session:
        argv = [
            "claude",
            "-p",
            "--model",
            self._config.engine.model,
            "--effort",
            self._config.engine.review_effort,
            "--max-budget-usd",
            str(self._config.engine.budget.review),
            "--json-schema",
            json.dumps(schema),
            # `--allowedTools` is variadic and must never be the last flag
            # before the prompt, or it swallows the prompt as another tool name.
            "--allowedTools",
            "Read,Grep,Glob",
            "--output-format",
            "json",
            *self._config.engine.extra_args,
            brief,
        ]
        result = self._exec.run(
            argv,
            cwd=worktree,
            timeout=self._config.engine.timeout_seconds,
            env=engine_environment(self.name),
        )
        cost, text = _payload(result.stdout)
        return Session(
            ok=result.ok and bool(text.strip()),
            text=text,
            cost=cost,
            timed_out=result.timed_out,
            detail=result.tail() if not result.ok else "",
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ClaudeEngine(model={self._config.engine.model!r}, at={self._exec.where!r})"


def _payload(raw: str) -> tuple[float | None, str]:
    """`(cost, result)` from a `--output-format json` envelope.

    Tolerant on purpose. A malformed envelope means the caller gets no cost and
    an empty result, which every caller already treats as a failure — raising
    here would turn a reporting problem into a lost run.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return (None, "")
    if not isinstance(payload, dict):
        return (None, "")
    cost = payload.get("total_cost_usd")
    return (cost if isinstance(cost, int | float) else None, str(payload.get("result", "")))
