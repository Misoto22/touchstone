"""Reconcile Touchstone's event ledger with live pull-request truth."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from touchstone.config import LoopConfig
from touchstone.execution import Executor
from touchstone.forge import ForgeUnavailable, OperationResult, PullState
from touchstone.ledger import FindingProjection, Ledger, LifecycleEvent


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


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    merged: tuple[int, ...] = ()
    closed: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    reaped: tuple[int, ...] = ()
    inconclusive: tuple[int, ...] = ()


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


@dataclass(frozen=True, slots=True)
class PublicationResult:
    outcome: str
    finding_id: str
    pr: int | None
    head_sha: str | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    finding_id: str
    pr: int
    decision: Literal["merge", "close"]
    reviewed_head_sha: str


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
    ) -> None:
        self._forge = forge
        self._ledger = ledger
        self._reap_after_hours = reap_after_hours
        self._executor = executor

    def resume(self, request: ResumeRequest) -> ResumeResult:
        """Apply an operator decision only to the exact commit they reviewed."""
        projection = self._ledger.projection(request.finding_id)
        if projection is None:
            return ResumeResult("held", request.pr, "the finding is absent from the ledger")
        if projection.state != "parked" or projection.pr != request.pr:
            return ResumeResult(
                "held",
                request.pr,
                f"the finding is {projection.state}, not the parked pull request #{request.pr}",
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

        if request.decision == "close":
            closed = self._forge.close(request.pr, "Closed by the operator through Touchstone.")
            if not closed.ok:
                return ResumeResult(
                    "held", request.pr, closed.detail or "could not close the pull request"
                )
            self._transition(projection, "closed", "the operator closed the parked draft")
            return ResumeResult("closed", request.pr)

        if pull.draft:
            ready = self._forge.mark_ready(request.pr)
            if not ready.ok:
                return ResumeResult(
                    "held", request.pr, ready.detail or "could not mark the draft ready"
                )
        armed = self._forge.arm_auto_merge(request.pr)
        if not armed.ok:
            return ResumeResult("held", request.pr, armed.detail or "could not enable auto-merge")
        self._transition(projection, "armed", "the operator approved the reviewed commit")
        return ResumeResult("armed", request.pr)

    def publish(self, request: PublicationRequest) -> PublicationResult:
        """Publish once, and project only forge operations that succeeded."""
        if self._executor is None:
            raise RuntimeError("publishing requires an executor")

        error = self._commit_and_push(request)
        if error:
            self._record(request, "proposed", detail=error)
            return PublicationResult("held", request.finding_id, None, None, error)

        head = self._git(request.worktree, ["rev-parse", "HEAD"])
        if not head:
            detail = "could not resolve the published commit"
            self._record(request, "proposed", detail=detail)
            return PublicationResult("held", request.finding_id, None, None, detail)

        park = request.risk != "low" or request.verdict != "approve"
        try:
            pull = self._forge.pull_for_branch(request.branch)
        except ForgeUnavailable:
            detail = "could not verify existing pull requests for the published branch"
            self._record(request, "proposed", head_sha=head, detail=detail)
            return PublicationResult("held", request.finding_id, None, head, detail)
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
            self._record(request, "proposed", head_sha=head, detail=detail)
            return PublicationResult("held", request.finding_id, None, head, detail)

        if park:
            if pull is not None and not pull.draft:
                drafted = self._forge.to_draft(number)
                if not drafted.ok:
                    detail = drafted.detail or "could not convert the pull request to a draft"
                    self._record(request, "proposed", pr=number, head_sha=head, detail=detail)
                    return PublicationResult("held", request.finding_id, number, head, detail)
            labelled = self._forge.add_label(number, request.escalation_label)
            if not labelled.ok:
                detail = labelled.detail or "could not add the escalation label"
                self._record(request, "proposed", pr=number, head_sha=head, detail=detail)
                return PublicationResult("held", request.finding_id, number, head, detail)
            detail = f"{request.risk} / {request.verdict}: {request.review_reason}"
            self._record(request, "parked", pr=number, head_sha=head, detail=detail)
            return PublicationResult("parked", request.finding_id, number, head, detail)

        armed = self._forge.arm_auto_merge(number)
        if not armed.ok:
            detail = armed.detail or "could not enable auto-merge"
            self._record(request, "proposed", pr=number, head_sha=head, detail=detail)
            return PublicationResult("held", request.finding_id, number, head, detail)
        detail = "independent review approved; auto-merge armed"
        self._record(request, "armed", pr=number, head_sha=head, detail=detail)
        return PublicationResult("armed", request.finding_id, number, head, detail)

    def reconcile(self, loop: LoopConfig, now: dt.datetime) -> ReconcileReport:
        merged: list[int] = []
        closed: list[int] = []
        failed: list[int] = []
        reaped: list[int] = []
        inconclusive: list[int] = []

        for projection in self._ledger.projections().values():
            if projection.loop not in {loop.name, "legacy"}:
                continue
            if projection.state not in {"armed", "parked"} or projection.pr is None:
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
                projection.state == "armed"
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
        )

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
            if request.author_name and request.author_email:
                argv += [
                    "-c",
                    f"user.name={request.author_name}",
                    "-c",
                    f"user.email={request.author_email}",
                ]
            argv += ["commit", "--quiet", "-m", request.commit_subject]
            if request.summary:
                argv += ["-m", request.summary]
            committed = self._executor.run(argv, timeout=180)
            if not committed.ok:
                return f"could not commit changes: {committed.tail()}"

        pushed = self._executor.run(
            ["git", "-C", worktree, "push", "--quiet", "-u", "origin", request.branch],
            timeout=180,
        )
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
            )
        )


def _age_hours(created_at: str, now: dt.datetime) -> float:
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    return max(0.0, (now - created.astimezone(dt.UTC)).total_seconds() / 3600)


def _pull_body(request: PublicationRequest) -> str:
    escalation = f"\n\nEscalated by the loop: {request.escalation}" if request.escalation else ""
    return f"""## What this changes

{request.summary}

## Why

{request.rationale}

## Risk

`{request.risk}`{escalation}

## Independent review

**{request.verdict}** — {request.review_reason}

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
