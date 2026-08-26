"""Step four: commit, open the pull request, and decide what happens to it.

Split into pieces the graph can call separately, because `park` is an
interrupt: the pull request has to exist before a person can be asked about
it, and what happens afterwards depends on their answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from touchstone.ledger import finding_id
from touchstone.lifecycle import (
    PublicationRequest,
    RepositoryLifecycle,
    ResumeRequest,
    auto_merge_unsupported,
)
from touchstone.nodes.context import current


def _request(state: dict[str, Any], context: Any) -> PublicationRequest:
    finding = state.get("finding", {})
    loop = context.loop(state["loop"])
    title = finding.get("title") or "Touchstone finding"
    # A hosted run supplies the publishing App's own bot identity, which the
    # configuration cannot know; a local run falls back to the configured pair.
    # Which of the two identities git records is the project's choice, and the
    # other one is credited in a trailer.
    hosted_bot = (
        (state["author_name"], state["author_email"])
        if state.get("author_name") and state.get("author_email")
        else None
    )
    author, coauthor = context.config.git.identities(bot=hosted_bot)
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
        author_name=author[0] if author else None,
        author_email=author[1] if author else None,
        coauthor_name=coauthor[0] if coauthor else None,
        coauthor_email=coauthor[1] if coauthor else None,
        auto_merge=loop.auto_merge,
        independently_verified=bool(state.get("independently_verified", False)),
        required_checks_declared=bool(context.config.forge.required_workflows),
        pre_staged=bool(state.get("pre_staged", False)),
        repository=context.config.forge.slug,
        isolated_push=bool(state.get("isolated_push", False)),
        # Measured by classify against the same worktree this publishes.
        paths=tuple(state.get("changed_paths") or ()),
    )


def _publish(state: dict[str, Any], *, validation_required: bool = True) -> dict[str, Any]:
    context = current()
    # `validation_required` is exactly the question auto-merge needs answered:
    # a hosted publication reaches here because a stage holding no model
    # credential already validated the candidate, and a local one because this
    # process is about to. Only the first is an independent check of the
    # model's own work.
    unsupported = auto_merge_unsupported(
        requested=context.loop(state["loop"]).auto_merge,
        independently_verified=not validation_required,
    )
    if unsupported:
        return {"outcome": "blocked", "pr": None, "notes": [unsupported]}
    if validation_required:
        from touchstone.validation import validate_affected

        loop = context.loop(state["loop"])
        validation = validate_affected(
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
                "outcome": "blocked",
                "pr": None,
                "notes": ["Validation blocked publication: " + "; ".join(details)],
            }
    lifecycle = RepositoryLifecycle(
        context.forge,
        context.ledger,
        reap_after_hours=context.config.forge.reap_after_hours,
        executor=context.executor,
    )
    return _publication_payload(lifecycle.publish(_request(state, context)))


def _publication_payload(result: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "outcome": result.outcome,
        "pr": result.pr,
        "finding_id": result.finding_id,
        "reviewed_head_sha": result.head_sha,
        "partial": result.partial,
        "branch": result.branch,
    }
    if result.outcome == "failed" and result.detail:
        payload["notes"] = [result.detail]
    return payload


def _publish_verified(state: dict[str, Any]) -> dict[str, Any]:
    """Mutate the forge only after a credential-free stage validated and staged the patch."""

    return _publish(state | {"independently_verified": True}, validation_required=False)


def merge(state: dict[str, Any]) -> dict[str, Any]:
    """Approved and low risk: open it and let GitHub merge it when checks pass."""
    return _publish(state)


def park(state: dict[str, Any]) -> dict[str, Any]:
    """Anything else: a draft, labelled for a person, and the thread waits."""
    return _publish(state)


def arm_merge(state: dict[str, Any]) -> dict[str, Any]:
    """A person approved. Resumed here rather than re-audited from scratch."""
    return _resume(state, "approve")


def record_closed(state: dict[str, Any]) -> dict[str, Any]:
    """A person said no. Recorded so the finding is not raised again."""
    return _resume(state, "close")


def record_reanalysis(state: dict[str, Any]) -> dict[str, Any]:
    return _resume(state, "reanalyze")


def _resume(state: dict[str, Any], decision: str) -> dict[str, Any]:
    context = current()
    number = state.get("pr")
    identifier = state.get("finding_id")
    reviewed_head = state.get("reviewed_head_sha")
    if number is None or not identifier or not reviewed_head:
        return {
            "outcome": "blocked",
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
            decision=decision,
            reviewed_head_sha=str(reviewed_head),
            lineage=str(identifier),
        )
    )
    outcome = result.outcome
    payload: dict[str, Any] = {"outcome": outcome, "pr": result.pr}
    if result.detail and result.outcome in {"held", "failed"}:
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
    loop = context.config.loop(state["loop"])
    from touchstone.validation import prepare, validate_affected

    preparation = prepare(
        context.config,
        loop.targets,
        context.executor,
        repository=Path(worktree),
    )
    if preparation.outcome == "blocked":
        return {
            "outcome": "blocked",
            "pr": None,
            "notes": ["Dry-run preparation blocked candidate validation."],
        }
    validation = validate_affected(
        context.config,
        loop.targets,
        context.executor,
        repository=Path(worktree),
    )
    if validation.blocked:
        return {
            "outcome": "blocked",
            "pr": None,
            "notes": ["Dry-run candidate failed configured Validation Gates."],
        }
    base = f"origin/{context.config.forge.default_branch}"

    diff = context.executor.run(["git", "-C", worktree, "diff", base], timeout=180).stdout
    target = Path(context.config.state_dir) / "dry-run.diff"
    target.write_text(diff, encoding="utf-8")

    stat = context.executor.run(
        ["git", "-C", worktree, "diff", "--shortstat", base], timeout=60
    ).stdout.strip()

    return {
        "outcome": "rehearsed",
        "pr": None,
        "notes": [
            f"would {would}",
            f"risk {state.get('risk')} / review {state.get('verdict', 'skipped')}",
            f"diff at {target} ({stat})",
        ],
    }
