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

    finding: dict[str, Any]
    risk: Literal["low", "medium", "high"]
    escalation: str

    verdict: Literal["approve", "reject", "skipped"]
    verdict_reason: str

    outcome: Literal["clean", "merging", "escalated", "held", "reaped", "inconclusive"]
    pr: int | None
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
    return "merge" if state.get("verdict") == "approve" else "park"


def park(state: LoopState) -> LoopState:
    """Open the pull request as a draft, then stop and wait for a person.

    The interrupt is the point. A bash loop parked a draft and exited, so a
    person's answer reached nothing — the next hour simply started over and the
    ledger row was all that carried forward. Here the thread is checkpointed at
    this node, and answering resumes it.
    """
    published = publish.park(state)
    decision = interrupt(
        {
            "pr": published["pr"],
            "risk": state.get("risk"),
            "verdict": state.get("verdict", "skipped"),
            "reason": state.get("verdict_reason", ""),
            "question": "Merge this, or close it? Resume the thread with 'merge' or 'close'.",
        }
    )
    if decision == "merge":
        return publish.arm_merge({**state, **published})
    return publish.record_closed({**state, **published})


def build():  # type: ignore[no-untyped-def]
    """The compiled graph. Give it a checkpointer to make `park` resumable."""
    graph = StateGraph(LoopState)

    graph.add_node("audit", audit.run)
    graph.add_node("classify", classify.run)
    graph.add_node("review", review.run)
    graph.add_node("merge", publish.merge)
    graph.add_node("park", park)

    graph.set_entry_point("audit")
    graph.add_conditional_edges("audit", _after_audit, {"classify": "classify", END: END})
    graph.add_conditional_edges("classify", _after_classify, {"review": "review", "park": "park"})
    graph.add_conditional_edges("review", _after_review, {"merge": "merge", "park": "park"})
    graph.add_edge("merge", END)
    graph.add_edge("park", END)
    return graph


__all__ = ["LoopState", "build", "park"]
