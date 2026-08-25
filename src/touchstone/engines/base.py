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
    *,
    base_url: str = "",
) -> dict[str, str]:
    """Return the minimal environment allowed to cross into a model process.

    `base_url` is configuration rather than a credential, so it travels with
    the rest of the environment. Claude reads its endpoint from
    `ANTHROPIC_BASE_URL`; Codex is told through its own provider settings and
    needs nothing here.
    """

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
    if base_url and engine == "claude":
        result["ANTHROPIC_BASE_URL"] = base_url
    return result


#: How an engine says it was present and thinking but could not act.
#:
#: A session that cannot write is not a session that found nothing, and telling
#: them apart cannot be left to the exit code. On a host that forbids
#: unprivileged uid mapping, bubblewrap cannot bring up loopback inside its
#: namespace, so the sandbox never starts and every write fails.
#:
#: The sandbox helper is not the part that hides this. Run directly in a
#: container that withholds unprivileged user namespaces, `codex sandbox` fails
#: the write and exits 1, saying why. It is `codex exec` that exits 0: each
#: refused write is one failed tool call, the model reasons past it, says in
#: plain words that it was blocked, and the agent loop ends normally. So the
#: transcript is not a convenient place to look for this — it is the only place
#: that carries it.
#:
#: Matched against the transcript, which is where the engine is honest. But the
#: transcript is also where the engine *quotes the repository*, and an audit of
#: permission-handling code discusses these phrases as its subject matter — the
#: first version matched a bare "Operation not permitted" and would have called
#: a perfectly good review of an auth module blocked. So each marker is either
#: a diagnostic no ordinary prose produces, or a pair that has to appear
#: together.
_SPECIFIC: tuple[tuple[str, str], ...] = (
    ("bwrap:", "the sandbox could not start"),
    ("Failed to write file", "a file write was refused"),
    ("Blocked by workspace infrastructure", "the engine reported it could not act"),
    ("sandbox_error", "the engine reported a sandbox error"),
)

#: Phrases that mean nothing alone and something together. "Operation not
#: permitted" is ordinary text in a diff about permissions; alongside a
#: namespace or seccomp failure it is a machine refusing the engine.
_CORROBORATING: tuple[tuple[tuple[str, ...], str], ...] = (
    (("Operation not permitted", "namespace"), "a namespace operation was refused"),
    (("Operation not permitted", "uid_map"), "uid mapping was refused"),
    (("Operation not permitted", "seccomp"), "a seccomp restriction refused the engine"),
    (("Permission denied", "sandbox"), "the sandbox was denied access"),
)


def blocked_reason(transcript: str) -> str | None:
    """Why this session could not act, or `None` if nothing says it could not.

    Returns a category of this function's own devising, never a slice of the
    transcript: the answer travels into notes, the event log and the ledger,
    and those are packaged into the hosted state snapshot. The transcript
    itself stays in `state_dir`, on the machine, for whoever has to explain the
    run.
    """
    for marker, reason in _SPECIFIC:
        if marker in transcript:
            return reason
    for markers, reason in _CORROBORATING:
        if all(marker in transcript for marker in markers):
            return reason
    return None


def keep(state_dir: str, name: str, text: str) -> None:
    """Persist what a session said, for whoever has to explain it later.

    A run that takes seven minutes and produces nothing is the hardest kind to
    diagnose, and the first time it happened there was no record at all: the
    ledger said `no finding file written` and the reasoning behind that had
    already been discarded. Cheap to keep, impossible to reconstruct.

    Written by the parent process into `state_dir`, never by the model's own
    process — under a hosted backend the child's `HOME` is redirected, and a
    transcript written there would land somewhere nobody thinks to look.
    """
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
    #: Set when the engine ran, thought, and could not act — as distinct from
    #: running and finding nothing. The two look identical from outside.
    blocked: str | None = None


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

    def author(
        self, brief: str, *, worktree: str, denied: tuple[str, ...], model: str = ""
    ) -> Session:
        """Run a session that may edit `worktree`.

        Its contract is the files it leaves behind, never its stdout, so a
        chatty model cannot corrupt the result.
        """

    def review(self, brief: str, *, worktree: str, schema: dict, model: str = "") -> Session:
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
