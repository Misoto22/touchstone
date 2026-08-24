"""Step four: commit, open the pull request, and decide what happens to it.

Split into pieces the graph can call separately, because `park` is an
interrupt: the pull request has to exist before a person can be asked about
it, and what happens afterwards depends on their answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from touchstone.ledger import finding_id
from touchstone.lifecycle import PublicationRequest, RepositoryLifecycle, ResumeRequest
from touchstone.nodes.context import current


def _request(state: dict[str, Any], context: Any) -> PublicationRequest:
    finding = state.get("finding", {})
    loop = context.loop(state["loop"])
    title = finding.get("title") or "Touchstone finding"
    return PublicationRequest(
        finding_id=state.get("finding_id") or finding_id(loop.name, title),
        loop=loop.name,
        branch=state["branch"],
        worktree=Path(state["worktree"]),
        base=context.config.forge.default_branch,
        label=loop.label,
        escalation_label=context.config.forge.escalation_label,
        risk=state.get("risk", "high"),
        verdict=state.get("verdict", "skipped"),
        title=title,
        commit_subject=finding.get("commit_subject") or "chore: address Touchstone finding",
        summary=finding.get("summary", ""),
        rationale=finding.get("rationale", ""),
        review_reason=state.get("verdict_reason", ""),
        escalation=state.get("escalation", ""),
        author_name=context.config.git.author_name,
        author_email=context.config.git.author_email,
    )


def _publish(state: dict[str, Any]) -> dict[str, Any]:
    context = current()
    from touchstone.validation import validate

    loop = context.loop(state["loop"])
    validation = validate(
        context.config,
        loop.targets,
        context.executor,
        repository=Path(state["worktree"]),
    )
    if validation.blocked:
        details = [
            f"{' '.join(result.argv)}: {result.reason}"
            for result in validation.results
            if not result.ok
        ]
        return {
            "outcome": "held",
            "pr": None,
            "notes": ["Validation blocked publication: " + "; ".join(details)],
        }
    lifecycle = RepositoryLifecycle(
        context.forge,
        context.ledger,
        reap_after_hours=context.config.forge.reap_after_hours,
        executor=context.executor,
    )
    result = lifecycle.publish(_request(state, context))
    outcome = {"armed": "merging", "parked": "escalated"}.get(result.outcome, "held")
    payload: dict[str, Any] = {
        "outcome": outcome,
        "pr": result.pr,
        "finding_id": result.finding_id,
        "reviewed_head_sha": result.head_sha,
    }
    if result.outcome == "held" and result.detail:
        payload["notes"] = [result.detail]
    return payload


def merge(state: dict[str, Any]) -> dict[str, Any]:
    """Approved and low risk: open it and let GitHub merge it when checks pass."""
    return _publish(state)


def park(state: dict[str, Any]) -> dict[str, Any]:
    """Anything else: a draft, labelled for a person, and the thread waits."""
    return _publish(state)


def arm_merge(state: dict[str, Any]) -> dict[str, Any]:
    """A person said merge. Resumed here rather than re-audited from scratch."""
    return _resume(state, "merge")


def record_closed(state: dict[str, Any]) -> dict[str, Any]:
    """A person said no. Recorded so the finding is not raised again."""
    return _resume(state, "close")


def _resume(state: dict[str, Any], decision: str) -> dict[str, Any]:
    context = current()
    number = state.get("pr")
    identifier = state.get("finding_id")
    reviewed_head = state.get("reviewed_head_sha")
    if number is None or not identifier or not reviewed_head:
        return {
            "outcome": "held",
            "pr": number,
            "notes": ["the parked checkpoint is missing its pull request identity"],
        }
    lifecycle = RepositoryLifecycle(
        context.forge,
        context.ledger,
        reap_after_hours=context.config.forge.reap_after_hours,
    )
    result = lifecycle.resume(
        ResumeRequest(
            finding_id=str(identifier),
            pr=int(number),
            decision="merge" if decision == "merge" else "close",
            reviewed_head_sha=str(reviewed_head),
        )
    )
    outcome = {"armed": "merging", "closed": "escalated"}.get(result.outcome, "held")
    payload: dict[str, Any] = {"outcome": outcome, "pr": result.pr}
    if result.detail and result.outcome == "held":
        payload["notes"] = [result.detail]
    return payload


def rehearse(state: dict[str, Any], *, would: str) -> dict[str, Any]:
    """What a real run would have done, and the diff it would have carried.

    Nothing reaches the forge. The diff is kept where it can be read after the
    worktree is torn down, because the whole point of a rehearsal is to look at
    it — and the worktree is gone by the time anyone thinks to.
    """
    context = current()
    worktree = state["worktree"]
    base = f"origin/{context.config.forge.default_branch}"

    diff = context.executor.run(["git", "-C", worktree, "diff", base], timeout=180).stdout
    target = Path(context.config.state_dir) / "dry-run.diff"
    target.write_text(diff, encoding="utf-8")

    stat = context.executor.run(
        ["git", "-C", worktree, "diff", "--shortstat", base], timeout=60
    ).stdout.strip()

    # Recorded, so the ledger is a complete account of what the loop did rather
    # than only of what it published. `rehearsed` is deliberately not in the
    # handled allowlist: a rehearsal disposes of nothing, and feeding its title
    # back as already-seen would hide a defect nobody has fixed.
    context.ledger.record(
        status="rehearsed",
        risk=state.get("risk"),
        pr=None,
        title=state.get("finding", {}).get("title", ""),
        detail=f"would {would}; review {state.get('verdict', 'skipped')}",
    )

    return {
        "outcome": "rehearsed",
        "pr": None,
        "notes": [
            f"would {would}",
            f"risk {state.get('risk')} / review {state.get('verdict', 'skipped')}",
            f"diff at {target} ({stat})",
        ],
    }
