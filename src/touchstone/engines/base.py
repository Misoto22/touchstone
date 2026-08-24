"""What a coding agent has to be able to do for this loop.

Two calls, and the loop's structure — the gates, the risk classes, the diff
guard, the independent review — belongs to none of them. That is the point of
the seam: the part that makes unattended merging survivable is not specific to
a vendor, and only two steps ever talk to a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from touchstone.execution import Executor

#: Things an engine says when it was present and thinking but could not act.
#:
#: A session that cannot write is not a session that found nothing, and telling
#: them apart cannot be left to the exit code: `codex exec` exits 0 after every
#: one of its file writes was refused. On a host that forbids unprivileged uid
#: mapping, bubblewrap cannot bring up loopback inside its namespace, so the
#: sandbox never starts and every write fails — for six hours the loop spent
#: seven minutes and 122k tokens per run, found a real defect each time, wrote
#: nothing, and recorded `clean`. Six identical entries saying all is well.
#:
#: Matched against the transcript rather than the exit status, because the
#: transcript is where the engine is honest: "Blocked by workspace
#: infrastructure. Every shell and file-write attempt failed."
BLOCKED_MARKERS: tuple[tuple[str, str], ...] = (
    ("bwrap:", "the sandbox could not start"),
    ("Failed to write file", "a file write was refused"),
    ("Blocked by workspace infrastructure", "the engine reported it could not act"),
    ("Operation not permitted", "an operation was refused by the host"),
)


def blocked_reason(transcript: str) -> str | None:
    """Why this session could not act, or `None` if nothing says it could not.

    Deliberately generous about what counts. A false positive costs one run
    recorded as `held` when it was merely clean; a false negative costs an
    unbounded number of runs recorded as clean when the engine was mute — and
    the second is what actually happened.
    """
    for marker, reason in BLOCKED_MARKERS:
        if marker in transcript:
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
