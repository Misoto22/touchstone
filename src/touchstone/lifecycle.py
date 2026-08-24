"""Reconcile Touchstone's event ledger with live pull-request truth."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from touchstone.config import LoopConfig
from touchstone.forge import OperationResult, PullState
from touchstone.ledger import FindingProjection, Ledger, LifecycleEvent


class LifecycleForge(Protocol):
    def pull(self, number: int) -> PullState | None: ...

    def close(self, number: int, comment: str) -> OperationResult: ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    merged: tuple[int, ...] = ()
    closed: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()
    reaped: tuple[int, ...] = ()
    inconclusive: tuple[int, ...] = ()


class RepositoryLifecycle:
    def __init__(self, forge: LifecycleForge, ledger: Ledger, *, reap_after_hours: int) -> None:
        self._forge = forge
        self._ledger = ledger
        self._reap_after_hours = reap_after_hours

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


def _age_hours(created_at: str, now: dt.datetime) -> float:
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    return max(0.0, (now - created.astimezone(dt.UTC)).total_seconds() / 3600)


__all__ = ["ReconcileReport", "RepositoryLifecycle"]
