"""What a coding agent has to be able to do for this loop.

Two calls, and the loop's structure — the gates, the risk classes, the diff
guard, the independent review — belongs to none of them. That is the point of
the seam: the part that makes unattended merging survivable is not specific to
a vendor, and only two steps ever talk to a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from touchstone.execution import Executor


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
