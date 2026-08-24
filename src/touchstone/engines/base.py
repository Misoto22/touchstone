"""What a coding agent has to be able to do for this loop.

Two calls, and the loop's structure — the gates, the risk classes, the diff
guard, the independent review — belongs to none of them. That is the point of
the seam: the part that makes unattended merging survivable is not specific to
a vendor, and only two steps ever talk to a model.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from touchstone.execution import Executor

_RUNTIME_ENVIRONMENT = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
}
_ENGINE_ENVIRONMENT = {
    "codex": {
        "AZURE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
    },
    "claude": {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
    },
}


def engine_environment(
    engine: str,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the minimal environment allowed to cross into a model process."""

    environment = os.environ if source is None else source
    allowed = _RUNTIME_ENVIRONMENT | _ENGINE_ENVIRONMENT.get(engine, set())
    result = {key: value for key, value in environment.items() if key in allowed and value}
    if environment.get("GITHUB_ACTIONS", "").lower() == "true":
        runner_temp = Path(environment.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
        home = runner_temp / "touchstone-model-home"
        home.mkdir(parents=True, exist_ok=True)
        result["HOME"] = str(home)
    elif environment.get("HOME"):
        result["HOME"] = environment["HOME"]
        config_key = "CODEX_HOME" if engine == "codex" else "CLAUDE_CONFIG_DIR"
        if environment.get(config_key):
            result[config_key] = environment[config_key]
    return result


def keep(state_dir: str, name: str, text: str) -> None:
    """Persist what a session said, for whoever has to explain it later.

    A run that takes seven minutes and produces nothing is the hardest kind to
    diagnose, and the first time it happened there was no record at all: the
    outcome said `no finding file written` and the reasoning behind that had
    already been discarded. Cheap to keep, impossible to reconstruct.
    """
    from pathlib import Path

    target = Path(state_dir) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Session:
    """One model call, and what came back."""

    ok: bool
    text: str
    #: Dollars, when the engine reports them. `None` is not zero — it means
    #: nobody knows, and writing an estimate in its place would be worse than
    #: leaving the field empty.
    cost: float | None
    timed_out: bool = False
    detail: str = ""


@runtime_checkable
class Engine(Protocol):
    name: str

    #: Whether a run can be told what it spent. Codex cannot, which makes the
    #: clock the only ceiling there and the spend invisible. Better stated in
    #: the type than discovered from a blank column.
    reports_cost: bool

    #: Whether the engine restrains writes by path. Claude takes a deny list,
    #: so the harness is unreachable at the permission layer; Codex takes a
    #: sandbox mode that grants the whole worktree, which leaves the diff check
    #: as the only thing between a stray edit and a protected path. It still
    #: catches one — after the fact, by escalating — and callers deserve to
    #: know which of the two they have.
    enforces_paths: bool

    def author(self, brief: str, *, worktree: str, denied: tuple[str, ...]) -> Session:
        """Run a session that may edit `worktree`.

        Its contract is the files it leaves behind, never its stdout, so a
        chatty model cannot corrupt the result.
        """

    def review(self, brief: str, *, worktree: str, schema: dict) -> Session:
        """Run a read-only session that returns an object matching `schema`.

        Read-only is enforced by the engine, not asked for politely: a reviewer
        able to widen the diff it is judging is not a reviewer. The schema is
        validated by the engine too, so a malformed answer is retried there
        rather than guessed at here — the previous implementation grepped free
        text for the last `approve` or `reject` to appear, which is right for
        most phrasings and guaranteed by none, immediately in front of an
        unattended production merge.
        """


def build(config, executor: Executor) -> Engine:  # type: ignore[no-untyped-def]
    """The engine a configuration asks for."""
    from touchstone.engines.claude import ClaudeEngine
    from touchstone.engines.codex import CodexEngine

    if config.engine.name == "claude":
        return ClaudeEngine(config, executor)
    return CodexEngine(config, executor)
