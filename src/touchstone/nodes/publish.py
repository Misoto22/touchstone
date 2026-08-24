"""Step four: commit, open the pull request, and decide what happens to it.

Split into pieces the graph can call separately, because `park` is an
interrupt: the pull request has to exist before a person can be asked about
it, and what happens afterwards depends on their answer.
"""

from __future__ import annotations

from typing import Any

from touchstone.nodes.context import current

_BODY = """## What this changes

{summary}

## Why

{rationale}

## Risk

`{risk}`{escalation}

## Independent review

**{verdict}** — {reason}

---

Opened without human involvement by the harness loop. Closing it is a valid
answer: the loop records the rejection and will not raise it again.
"""


def _commit_and_push(state: dict[str, Any]) -> bool:
    context = current()
    worktree = state["worktree"]
    finding = state.get("finding", {})
    subject = finding.get("commit_subject") or "chore: harness loop finding"
    body = finding.get("summary", "")

    for argv in (
        ["git", "-C", worktree, "add", "-A"],
        [
            "git",
            "-C",
            worktree,
            "-c",
            "user.name=Henry Chen",
            "-c",
            "user.email=henrycxw@gmail.com",
            "commit",
            "--quiet",
            "-m",
            f"{subject}\n\n{body}\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
        ],
        ["git", "-C", worktree, "push", "--quiet", "-u", "origin", state["branch"]],
    ):
        if not context.executor.run(argv, timeout=180).ok:
            return False
    return True


def _open(state: dict[str, Any]) -> int | None:
    context = current()
    loop = context.loop(state["loop"])
    finding = state.get("finding", {})
    escalation = state.get("escalation", "")
    body = _BODY.format(
        summary=finding.get("summary", ""),
        rationale=finding.get("rationale", ""),
        risk=state.get("risk", "high"),
        escalation=f"\n\nEscalated by the loop: {escalation}" if escalation else "",
        verdict=state.get("verdict", "skipped"),
        reason=state.get("verdict_reason", ""),
    )
    return context.forge.create_pull(
        base=context.config.forge.default_branch,
        head=state["branch"],
        title=finding.get("commit_subject") or "chore: harness loop finding",
        body=body,
        label=loop.label,
    )


def merge(state: dict[str, Any]) -> dict[str, Any]:
    """Approved and low risk: open it and let GitHub merge it when checks pass."""
    context = current()
    if not _commit_and_push(state):
        return {"outcome": "held", "notes": ["could not push the branch"]}
    number = _open(state)
    if number is None:
        return {"outcome": "held", "notes": ["could not open the pull request"]}

    context.forge.arm_auto_merge(number)
    context.ledger.record(
        status="merging",
        risk=state.get("risk"),
        pr=number,
        title=state.get("finding", {}).get("title", ""),
        detail="approved, auto-merge armed",
    )
    return {"outcome": "merging", "pr": number}


def park(state: dict[str, Any]) -> dict[str, Any]:
    """Anything else: a draft, labelled for a person, and the thread waits."""
    context = current()
    if not _commit_and_push(state):
        return {"outcome": "held", "pr": None, "notes": ["could not push the branch"]}
    number = _open(state)
    if number is None:
        return {"outcome": "held", "pr": None, "notes": ["could not open the pull request"]}

    context.forge.to_draft(number)
    context.forge.add_label(number, context.config.forge.escalation_label)
    context.ledger.record(
        status="escalated",
        risk=state.get("risk"),
        pr=number,
        title=state.get("finding", {}).get("title", ""),
        detail=(
            f"{state.get('risk')} / {state.get('verdict', 'skipped')}: "
            f"{state.get('verdict_reason', '')}"
        ),
    )
    return {"outcome": "escalated", "pr": number}


def arm_merge(state: dict[str, Any]) -> dict[str, Any]:
    """A person said merge. Resumed here rather than re-audited from scratch."""
    context = current()
    number = state.get("pr")
    if number is None:
        return {"outcome": "held", "notes": ["asked to merge, but no pull request exists"]}
    context.forge.arm_auto_merge(int(number))
    context.ledger.record(
        status="merging",
        risk=state.get("risk"),
        pr=number,
        title=state.get("finding", {}).get("title", ""),
        detail="a person approved the parked draft",
    )
    return {"outcome": "merging", "pr": number}


def record_closed(state: dict[str, Any]) -> dict[str, Any]:
    """A person said no. Recorded so the finding is not raised again."""
    context = current()
    number = state.get("pr")
    if number is not None:
        context.forge.close(int(number), "Closed by the operator from the harness loop.")
    context.ledger.record(
        status="escalated",
        risk=state.get("risk"),
        pr=number,
        title=state.get("finding", {}).get("title", ""),
        detail="a person closed the parked draft",
    )
    return {"outcome": "escalated", "pr": number}
