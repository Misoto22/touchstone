"""Reconcile Touchstone's event ledger with live pull-request truth."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from touchstone.config import LoopConfig
from touchstone.execution import Executor
from touchstone.forge import ForgeUnavailable, OperationResult, PullState
from touchstone.ledger import FindingProjection, Ledger, LifecycleEvent
from touchstone.outcomes import ChangeState, ResumeDecision


class LifecycleForge(Protocol):
    def pull(self, number: int) -> PullState | None: ...

    def pull_for_branch(self, branch: str) -> PullState | None: ...

    def create_pull(
        self,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        label: str,
        draft: bool = False,
    ) -> int | None: ...

    def arm_auto_merge(self, number: int) -> OperationResult: ...

    def to_draft(self, number: int) -> OperationResult: ...

    def mark_ready(self, number: int) -> OperationResult: ...

    def add_label(self, number: int, label: str) -> OperationResult: ...

    def close(self, number: int, comment: str) -> OperationResult: ...

    def branch_exists(self, branch: str) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    merged: tuple[int, ...] = ()
    closed: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    reaped: tuple[int, ...] = ()
    inconclusive: tuple[int, ...] = ()
    partial_resolved: tuple[str, ...] = ()
    partial_unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    finding_id: str
    loop: str
    branch: str
    worktree: Path
    base: str
    label: str
    escalation_label: str
    risk: str
    verdict: str
    title: str
    commit_subject: str
    summary: str
    rationale: str
    review_reason: str
    escalation: str = ""
    author_name: str | None = None
    author_email: str | None = None
    pre_staged: bool = False
    repository: str = ""
    isolated_push: bool = False


@dataclass(frozen=True, slots=True)
class PublicationResult:
    outcome: str
    finding_id: str
    pr: int | None
    head_sha: str | None
    detail: str = ""
    partial: bool = False
    branch: str = ""


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    finding_id: str
    pr: int
    decision: ResumeDecision | str
    reviewed_head_sha: str
    lineage: str = ""


@dataclass(frozen=True, slots=True)
class ResumeResult:
    outcome: str
    pr: int
    detail: str = ""


class RepositoryLifecycle:
    def __init__(
        self,
        forge: LifecycleForge,
        ledger: Ledger,
        *,
        reap_after_hours: int,
        executor: Executor | None = None,
        escalation_label: str = "",
    ) -> None:
        self._forge = forge
        self._ledger = ledger
        self._reap_after_hours = reap_after_hours
        self._executor = executor
        self._escalation_label = escalation_label

    def resume(self, request: ResumeRequest) -> ResumeResult:
        """Apply an operator decision only to the exact commit they reviewed."""
        projection = self._ledger.projection(request.finding_id)
        if projection is None:
            return ResumeResult("held", request.pr, "the finding is absent from the ledger")
        if projection.state != ChangeState.AWAITING_HUMAN or projection.pr != request.pr:
            return ResumeResult(
                "held",
                request.pr,
                f"the finding is {projection.state}, not the parked pull request #{request.pr}",
            )
        if not request.lineage or request.lineage != projection.finding_id:
            return ResumeResult(
                "held",
                request.pr,
                "the resume lineage does not match the exact parked candidate",
            )

        pull = self._forge.pull(request.pr)
        if pull is None:
            return ResumeResult("held", request.pr, "could not verify the live pull request")
        if pull.merged_at:
            self._transition(projection, "merged", "GitHub reports the pull request merged")
            return ResumeResult("merged", request.pr, "the pull request is already merged")
        if pull.closed:
            self._transition(projection, "closed", "GitHub reports the pull request closed")
            return ResumeResult("closed", request.pr, "the pull request is already closed")
        if not request.reviewed_head_sha or pull.head_sha != request.reviewed_head_sha:
            return ResumeResult(
                "held",
                request.pr,
                "the pull request head changed after it was parked; review the new commit first",
            )

        try:
            decision = ResumeDecision(request.decision)
        except ValueError:
            return ResumeResult("failed", request.pr, "unsupported resume decision")

        if decision in {ResumeDecision.CLOSE, ResumeDecision.REANALYZE}:
            comment = (
                "Closed for reanalysis from current default-branch state."
                if decision == ResumeDecision.REANALYZE
                else "Closed by the operator through Touchstone."
            )
            closed = self._forge.close(request.pr, comment)
            if not closed.ok:
                return ResumeResult(
                    "held", request.pr, closed.detail or "could not close the pull request"
                )
            self._transition(projection, "closed", "the operator closed the parked draft")
            return ResumeResult(
                "reanalyze" if decision == ResumeDecision.REANALYZE else "closed",
                request.pr,
            )

        if pull.draft:
            ready = self._forge.mark_ready(request.pr)
            if not ready.ok:
                return ResumeResult(
                    "held", request.pr, ready.detail or "could not mark the draft ready"
                )
        self._transition(
            projection,
            "awaiting_checks",
            "the operator approved the reviewed commit; auto-merge remains disabled",
        )
        return ResumeResult("awaiting_checks", request.pr)

    def publish(self, request: PublicationRequest) -> PublicationResult:
        """Publish once, and project only forge operations that succeeded."""
        if self._executor is None:
            raise RuntimeError("publishing requires an executor")

        error = self._commit_and_push(request)
        if error:
            self._record(request, "failed", branch=request.branch, partial=True, detail=error)
            return PublicationResult(
                "failed",
                request.finding_id,
                None,
                None,
                error,
                partial=True,
                branch=request.branch,
            )

        head = self._git(request.worktree, ["rev-parse", "HEAD"])
        if not head:
            detail = "could not resolve the published commit"
            self._record(
                request,
                "failed",
                branch=request.branch,
                partial=True,
                detail=detail,
            )
            return PublicationResult(
                "failed",
                request.finding_id,
                None,
                None,
                detail,
                partial=True,
                branch=request.branch,
            )

        park = request.risk != "low" or request.verdict != "approve"
        try:
            pull = self._forge.pull_for_branch(request.branch)
        except ForgeUnavailable:
            detail = "could not verify existing pull requests for the published branch"
            self._record(
                request,
                "failed",
                head_sha=head,
                branch=request.branch,
                partial=True,
                detail=detail,
            )
            return PublicationResult(
                "failed",
                request.finding_id,
                None,
                head,
                detail,
                partial=True,
                branch=request.branch,
            )
        number = (
            pull.number
            if pull is not None
            else self._forge.create_pull(
                base=request.base,
                head=request.branch,
                title=request.commit_subject,
                body=_pull_body(request),
                label=request.label,
                draft=park,
            )
        )
        if number is None:
            detail = "could not open or find the pull request"
            self._record(
                request,
                "failed",
                head_sha=head,
                branch=request.branch,
                partial=True,
                detail=detail,
            )
            return PublicationResult(
                "failed",
                request.finding_id,
                None,
                head,
                detail,
                partial=True,
                branch=request.branch,
            )

        if park:
            if pull is not None and not pull.draft:
                drafted = self._forge.to_draft(number)
                if not drafted.ok:
                    detail = drafted.detail or "could not convert the pull request to a draft"
                    self._record(
                        request,
                        "failed",
                        pr=number,
                        head_sha=head,
                        branch=request.branch,
                        partial=True,
                        detail=detail,
                    )
                    return PublicationResult(
                        "failed",
                        request.finding_id,
                        number,
                        head,
                        detail,
                        partial=True,
                        branch=request.branch,
                    )
            labelled = self._forge.add_label(number, request.escalation_label)
            if not labelled.ok:
                detail = labelled.detail or "could not add the escalation label"
                self._record(
                    request,
                    "failed",
                    pr=number,
                    head_sha=head,
                    branch=request.branch,
                    partial=True,
                    detail=detail,
                )
                return PublicationResult(
                    "failed",
                    request.finding_id,
                    number,
                    head,
                    detail,
                    partial=True,
                    branch=request.branch,
                )
            detail = f"{request.risk} / {request.verdict}: {request.review_reason}"
            # The branch is part of the record, not just of a failure. A resume
            # verifies the live pull request against the branch and head it was
            # parked with, so omitting it here made every normally parked draft
            # impossible to approve.
            self._record(
                request,
                "awaiting_human",
                pr=number,
                head_sha=head,
                branch=request.branch,
                detail=detail,
            )
            return PublicationResult(
                "awaiting_human",
                request.finding_id,
                number,
                head,
                detail,
                branch=request.branch,
            )

        detail = "independent review approved; pull request awaits checks and human merge"
        self._record(
            request,
            "awaiting_checks",
            pr=number,
            head_sha=head,
            branch=request.branch,
            detail=detail,
        )
        return PublicationResult(
            "awaiting_checks",
            request.finding_id,
            number,
            head,
            detail,
            branch=request.branch,
        )

    def reconcile(self, loop: LoopConfig, now: dt.datetime) -> ReconcileReport:
        merged: list[int] = []
        closed: list[int] = []
        failed: list[int] = []
        reaped: list[int] = []
        inconclusive: list[int] = []
        partial_resolved: list[str] = []
        partial_unresolved: list[str] = []

        for projection in self._ledger.projections().values():
            if projection.loop not in {loop.name, "legacy"}:
                continue
            if projection.partial and projection.state == ChangeState.FAILED:
                try:
                    pull = (
                        self._forge.pull(projection.pr)
                        if projection.pr is not None
                        else self._forge.pull_for_branch(projection.branch)
                        if projection.branch
                        else None
                    )
                except ForgeUnavailable:
                    partial_unresolved.append(projection.branch or projection.finding_id)
                    continue
                if pull is not None:
                    missing = self._missing_publication_labels(loop, pull)
                    if missing:
                        # The pull request exists but publication stopped before
                        # it reached an operator's queue. Finish that exact step
                        # rather than recording a park that nobody can see.
                        missing = self._apply_missing_labels(pull, missing)
                    if missing:
                        partial_unresolved.append(projection.branch or str(pull.number))
                        continue
                    self._ledger.append(
                        LifecycleEvent(
                            finding_id=projection.finding_id,
                            state=(
                                ChangeState.AWAITING_HUMAN
                                if pull.draft
                                else ChangeState.AWAITING_CHECKS
                            ),
                            title=projection.title,
                            loop=projection.loop,
                            risk=projection.risk,
                            pr=pull.number,
                            head_sha=pull.head_sha,
                            detail="reconciled a partial remote publication",
                            branch=pull.branch,
                        )
                    )
                    partial_resolved.append(projection.branch or str(pull.number))
                    continue
                if projection.branch and self._forge.branch_exists(projection.branch) is False:
                    self._transition(
                        projection,
                        "closed",
                        "the partial remote branch no longer exists",
                    )
                    partial_resolved.append(projection.branch)
                else:
                    partial_unresolved.append(projection.branch or projection.finding_id)
                continue
            if (
                projection.state
                not in {
                    ChangeState.AWAITING_CHECKS,
                    ChangeState.AWAITING_HUMAN,
                }
                or projection.pr is None
            ):
                continue
            pull = self._forge.pull(projection.pr)
            if pull is None:
                inconclusive.append(projection.pr)
                continue
            if pull.merged_at:
                self._transition(projection, "merged", "GitHub reports the pull request merged")
                merged.append(projection.pr)
                continue
            if pull.closed:
                self._transition(projection, "closed", "GitHub reports the pull request closed")
                closed.append(projection.pr)
                continue
            if (
                projection.state == ChangeState.AWAITING_CHECKS
                and not pull.draft
                and pull.check_state == "failure"
                and _age_hours(pull.created_at, now) >= self._reap_after_hours
            ):
                result = self._forge.close(
                    projection.pr,
                    "Closed by Touchstone after configured checks remained failed.",
                )
                if result.ok:
                    self._transition(
                        projection,
                        "reaped",
                        f"failed checks exceeded {self._reap_after_hours} hours",
                    )
                    reaped.append(projection.pr)
                else:
                    failed.append(projection.pr)

        return ReconcileReport(
            tuple(merged),
            tuple(closed),
            tuple(failed),
            tuple(reaped),
            tuple(inconclusive),
            tuple(partial_resolved),
            tuple(partial_unresolved),
        )

    def _missing_publication_labels(
        self,
        loop: LoopConfig,
        pull: PullState,
    ) -> tuple[str, ...]:
        """Name the labels a complete publication would have applied.

        An existing pull request is not proof of a finished publication. A park
        that failed at its labelling step leaves a draft that carries the Loop
        label and holds the slot, but not the escalation label that puts it in
        an operator's queue.
        """

        expected = [loop.label]
        if pull.draft:
            expected.append(self._escalation_label)
        present = set(pull.labels)
        return tuple(name for name in expected if name and name not in present)

    def _apply_missing_labels(self, pull: PullState, missing: tuple[str, ...]) -> tuple[str, ...]:
        """Add the labels publication could not, and report what still failed."""

        remaining = []
        for name in missing:
            if not self._forge.add_label(pull.number, name).ok:
                remaining.append(name)
        return tuple(remaining)

    def _transition(self, projection: FindingProjection, state: str, detail: str) -> None:
        self._ledger.append(
            LifecycleEvent(
                finding_id=projection.finding_id,
                state=state,  # type: ignore[arg-type]
                title=projection.title,
                loop=projection.loop,
                risk=projection.risk,
                pr=projection.pr,
                head_sha=projection.head_sha,
                detail=detail,
            )
        )

    def _commit_and_push(self, request: PublicationRequest) -> str:
        assert self._executor is not None
        worktree = str(request.worktree)
        if not request.pre_staged:
            added = self._executor.run(["git", "-C", worktree, "add", "-A"], timeout=180)
            if not added.ok:
                return f"could not stage changes: {added.tail()}"

        staged = self._executor.run(
            ["git", "-C", worktree, "diff", "--cached", "--quiet"], timeout=60
        )
        if staged.code not in {0, 1} or staged.timed_out:
            return f"could not inspect staged changes: {staged.tail()}"
        if staged.code == 1:
            argv = ["git", "-C", worktree]
            environment = None
            if request.isolated_push:
                argv += ["-c", "core.hooksPath=/dev/null"]
                environment = _isolated_git_environment()
            if request.author_name and request.author_email:
                argv += [
                    "-c",
                    f"user.name={request.author_name}",
                    "-c",
                    f"user.email={request.author_email}",
                ]
            argv += ["commit", "--quiet", "--no-verify", "-m", request.commit_subject]
            if request.summary:
                argv += ["-m", request.summary]
            committed = self._executor.run(argv, timeout=180, env=environment)
            if not committed.ok:
                return f"could not commit changes: {committed.tail()}"

        push = ["git", "-C", worktree]
        environment = None
        destination = "origin"
        refspec = request.branch
        if request.isolated_push:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", request.repository):
                return "could not push the branch: repository identity is invalid"
            push += [
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
            ]
            environment = _isolated_git_environment()
            destination = f"https://github.com/{request.repository}.git"
            refspec = f"HEAD:refs/heads/{request.branch}"
        push += [
            "push",
            "--quiet",
            "--no-verify",
            "-u",
            destination,
            refspec,
        ]
        pushed = self._executor.run(push, timeout=180, env=environment)
        return "" if pushed.ok else f"could not push the branch: {pushed.tail()}"

    def _git(self, worktree: Path, argv: list[str]) -> str:
        assert self._executor is not None
        result = self._executor.run(["git", "-C", str(worktree), *argv], timeout=60)
        return result.stdout.strip() if result.ok else ""

    def _record(
        self,
        request: PublicationRequest,
        state: str,
        *,
        pr: int | None = None,
        head_sha: str | None = None,
        detail: str = "",
        branch: str = "",
        partial: bool = False,
    ) -> None:
        self._ledger.append(
            LifecycleEvent(
                finding_id=request.finding_id,
                state=state,  # type: ignore[arg-type]
                title=request.title,
                loop=request.loop,
                risk=request.risk,
                pr=pr,
                head_sha=head_sha,
                detail=detail,
                branch=branch,
                partial=partial,
            )
        )


def _isolated_git_environment() -> dict[str, str]:
    allowed = {
        "GH_TOKEN",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
    }
    result = {key: value for key, value in os.environ.items() if key in allowed and value}
    result["GIT_CONFIG_NOSYSTEM"] = "1"
    result["GIT_TERMINAL_PROMPT"] = "0"
    return result


def _age_hours(created_at: str, now: dt.datetime) -> float:
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    return max(0.0, (now - created.astimezone(dt.UTC)).total_seconds() / 3600)


def _review_line(request: PublicationRequest) -> str:
    """What the review said, or why there is nothing for it to have said.

    A change above `low` never reaches the reviewer — nothing it could answer
    would let the change merge unattended, so paying for the session would buy
    nothing. That produced an empty verdict, which the body rendered as
    `**skipped** —` with no reason: identical to a review that ran and failed.
    Two pull requests were read that way, including by me.
    """
    if request.verdict in {"approve", "reject"}:
        return f"**{request.verdict}** — {request.review_reason}"
    if request.risk != "low":
        return (
            f"**not reviewed** — a `{request.risk}` change waits for a person "
            "whatever a reviewer would say, so none was asked."
        )
    reason = request.review_reason or "no reason was recorded, which is itself a defect"
    return f"**inconclusive** — {reason}"


def _pull_body(request: PublicationRequest) -> str:
    escalation = f"\n\nEscalated by the loop: {request.escalation}" if request.escalation else ""
    review_line = _review_line(request)
    return f"""## What this changes

{request.summary}

## Why

{request.rationale}

## Risk

`{request.risk}`{escalation}

## Independent review

{review_line}

---

Opened by Touchstone. Closing the pull request is a valid operator decision.
"""


__all__ = [
    "PublicationRequest",
    "PublicationResult",
    "ReconcileReport",
    "RepositoryLifecycle",
    "ResumeRequest",
    "ResumeResult",
]
