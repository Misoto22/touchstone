"""Stable machine contracts for runs, changes, and operator decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    NO_CHANGE = "no_change"
    BLOCKED = "blocked"
    FAILED = "failed"
    REHEARSED = "rehearsed"


class ChangeState(StrEnum):
    PROPOSED = "proposed"
    AWAITING_HUMAN = "awaiting_human"
    AWAITING_CHECKS = "awaiting_checks"
    MERGED = "merged"
    CLOSED = "closed"
    REAPED = "reaped"
    FAILED = "failed"


class ResumeDecision(StrEnum):
    APPROVE = "approve"
    CLOSE = "close"
    REANALYZE = "reanalyze"


_EXIT_CODES = {
    RunOutcome.COMPLETED: 0,
    RunOutcome.NO_CHANGE: 0,
    RunOutcome.REHEARSED: 0,
    RunOutcome.BLOCKED: 3,
    RunOutcome.FAILED: 1,
}


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: RunOutcome
    lifecycle: ChangeState | None = None
    reason_code: str = ""
    detail: str = ""
    pr_url: str = ""
    pr_number: int | None = None
    candidate_id: str = ""
    partial: bool = False
    retryable: bool = False

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self.outcome]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "outcome": self.outcome.value,
        }
        if self.lifecycle is not None:
            payload["lifecycle"] = self.lifecycle.value
        for key in ("reason_code", "detail", "pr_url", "candidate_id"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        if self.pr_number is not None:
            payload["pr_number"] = self.pr_number
        payload["partial"] = self.partial
        payload["retryable"] = self.retryable
        payload["exit_code"] = self.exit_code
        return payload


def from_legacy_outcome(
    value: str,
    *,
    dry_run: bool = False,
    paused: bool = False,
    pr: int | None = None,
    detail: str = "",
    partial: bool = False,
) -> RunResult:
    """Map a graph outcome onto the run contract the exit code reports.

    Order matters. A parked thread is not automatically a successful run: a
    publication that pushed a branch and opened a pull request but failed at a
    later step still parks, because the pull request exists and the graph asks a
    person about it. Reading `paused` first reported that run as `completed`
    with exit 0, so every monitor built on exit codes -- systemd `OnFailure=`,
    a scheduled job that opens an issue on failure -- stayed silent through it.
    An operation that should have succeeded and did not is a failure whatever
    the graph did afterwards.
    """

    if dry_run or value == "rehearsed":
        return RunResult(RunOutcome.REHEARSED, detail=detail)
    if partial:
        return RunResult(
            RunOutcome.FAILED,
            lifecycle=ChangeState.FAILED,
            reason_code="partial-publication",
            detail=detail,
            pr_number=pr,
            partial=True,
            retryable=True,
        )
    if value == "clean":
        return RunResult(RunOutcome.NO_CHANGE, detail=detail)
    if value in {"inconclusive"}:
        return RunResult(
            RunOutcome.FAILED,
            reason_code="contract-inconclusive",
            detail=detail,
            retryable=True,
        )
    if value in {"held", "blocked"}:
        return RunResult(RunOutcome.BLOCKED, reason_code="safety-gate", detail=detail)
    if value == "failed":
        return RunResult(
            RunOutcome.FAILED,
            reason_code="publication",
            detail=detail,
            pr_number=pr,
            retryable=True,
        )
    if value in {"awaiting_human", "escalated", "parked"} or paused:
        return RunResult(
            RunOutcome.COMPLETED,
            lifecycle=ChangeState.AWAITING_HUMAN,
            pr_number=pr,
            detail=detail,
        )
    if value in {"awaiting_checks", "merging", "armed"}:
        return RunResult(
            RunOutcome.COMPLETED,
            lifecycle=ChangeState.AWAITING_CHECKS,
            pr_number=pr,
            detail=detail,
        )
    return RunResult(
        RunOutcome.FAILED,
        reason_code="unknown-outcome",
        detail=detail or value,
        retryable=True,
    )


__all__ = [
    "ChangeState",
    "ResumeDecision",
    "RunOutcome",
    "RunResult",
    "from_legacy_outcome",
]
