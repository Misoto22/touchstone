"""The four steps, and what happens between them.

Deliberately not the whole loop. The gates, the lock and the worktree are
filesystem semantics and early exits; wrapping them in nodes would produce a
harder-to-debug equivalent of an `if`. What belongs in a graph is the part with
branches worth drawing:

    audit ─┬─ nothing found ──────────────────────────────── done
           └─ found ── classify ─┬─ low ── review ─┬─ approve ── publish(merge)
                                 │                 └─ reject ─── publish(park)
                                 └─ medium/high ─────────────── publish(park)

And one property no bash version could have: `publish(park)` is an interrupt,
not an exit. The state is checkpointed to SQLite and the graph stops. When a
person answers on the pull request, the same thread resumes at that node
instead of starting a fresh twenty-minute audit — which is what R-HAR-5's
"waits for a person" should always have meant.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from touchstone.nodes import audit, classify, publish, review


class LoopState(TypedDict, total=False):
    """What travels between the four steps.

    Flat and JSON-shaped because it is checkpointed: anything that cannot be
    serialised cannot survive an interrupt, and an interrupt that loses state
    is a rerun wearing a different name.
    """

    loop: str
    worktree: str
    branch: str
    #: A rehearsal runs the audit, the classification and the review for real
    #: and stops before anything reaches the forge. It has to travel in the
    #: state rather than being closed over, because the nodes that publish are
    #: the ones that must not — and passing it only to the gates meant a dry
    #: run opened a real pull request while reporting `clean`.
    dry_run: bool

    finding: dict[str, Any]
    risk: Literal["low", "medium", "high"]
    escalation: str

    verdict: Literal["approve", "reject", "skipped"]
    verdict_reason: str
    #: What a person answered at `await_person`, once they have.
    decision: Literal["approve", "close", "reanalyze"]

    outcome: Literal[
        "clean",
        "awaiting_checks",
        "awaiting_human",
        "blocked",
        "failed",
        "rehearsed",
        "inconclusive",
    ]
    pr: int | None
    finding_id: str
    reviewed_head_sha: str
    partial: bool
    cost: Annotated[list[float | None], lambda a, b: a + b]
    notes: Annotated[list[str], lambda a, b: a + b]


def _after_audit(state: LoopState) -> str:
    return "classify" if state.get("finding", {}).get("status") == "proposed" else END


def _after_classify(state: LoopState) -> str:
    """Only `low` is worth a review.

    Anything higher goes to a person whatever a reviewer would have said, and
    spending a second session to be told so costs a seventh of an audit for an
    answer that changes nothing.
    """
    return "review" if state.get("risk") == "low" else "park"


def _after_review(state: LoopState) -> str:
    if state.get("outcome") == "inconclusive":
        return END
    return "merge" if state.get("verdict") == "approve" else "park"


def _merge(state: LoopState) -> LoopState:
    if state.get("dry_run"):
        return publish.rehearse(state, would="merge")
    return publish.merge(state)


def park(state: LoopState) -> LoopState:
    """Open the draft. Nothing else — the waiting is the next node's job.

    Separate from the interrupt on purpose, and this is not stylistic. A node
    that interrupts re-executes from its first line when the thread resumes, so
    publishing and waiting in one node opens a second pull request every time a
    person answers. Splitting them puts a checkpoint between the side effect
    and the wait, which is what makes the side effect happen once.
    """
    if state.get("dry_run"):
        return publish.rehearse(state, would="park")
    return publish.park(state)


def await_person(state: LoopState) -> LoopState:
    """Stop, and let a person decide.

    The interrupt is the point of the whole rewrite. A shell loop parked a
    draft and exited, so an answer reached nothing: the next hour started over
    and paid for another audit to reach the same conclusion. Here the thread is
    checkpointed and resuming continues from this line.
    """
    if state.get("dry_run") or state.get("pr") is None:
        return {}

    decision = interrupt(
        {
            "pr": state["pr"],
            "risk": state.get("risk"),
            "verdict": state.get("verdict", "skipped"),
            "reason": state.get("verdict_reason", ""),
            "question": "Approve, close, or reanalyze this exact reviewed candidate?",
        }
    )
    return {"decision": decision if decision in {"approve", "close", "reanalyze"} else "close"}


def _after_person(state: LoopState) -> str:
    if state.get("decision") == "approve":
        return "arm_merge"
    if state.get("decision") == "close":
        return "record_closed"
    if state.get("decision") == "reanalyze":
        return "record_reanalysis"
    return END


def build():  # type: ignore[no-untyped-def]
    """The compiled graph. Give it a checkpointer to make `park` resumable."""
    graph = StateGraph(LoopState)

    graph.add_node("audit", audit.run)
    graph.add_node("classify", classify.run)
    graph.add_node("review", review.run)
    graph.add_node("merge", _merge)
    graph.add_node("park", park)
    graph.add_node("await_person", await_person)
    graph.add_node("arm_merge", publish.arm_merge)
    graph.add_node("record_closed", publish.record_closed)
    graph.add_node("record_reanalysis", publish.record_reanalysis)

    graph.set_entry_point("audit")
    graph.add_conditional_edges("audit", _after_audit, {"classify": "classify", END: END})
    graph.add_conditional_edges("classify", _after_classify, {"review": "review", "park": "park"})
    graph.add_conditional_edges(
        "review", _after_review, {"merge": "merge", "park": "park", END: END}
    )
    graph.add_edge("merge", END)
    graph.add_edge("park", "await_person")
    graph.add_conditional_edges(
        "await_person",
        _after_person,
        {
            "arm_merge": "arm_merge",
            "record_closed": "record_closed",
            "record_reanalysis": "record_reanalysis",
            END: END,
        },
    )
    graph.add_edge("arm_merge", END)
    graph.add_edge("record_closed", END)
    graph.add_edge("record_reanalysis", END)
    return graph


__all__ = ["LoopState", "await_person", "build", "park"]
