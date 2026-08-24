"""Append-only finding lifecycle events and their current projections."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from touchstone.outcomes import ChangeState

LifecycleState = ChangeState

SUPPRESSED = frozenset(
    {ChangeState.AWAITING_CHECKS, ChangeState.AWAITING_HUMAN, ChangeState.MERGED}
)
LEGACY_STATES: dict[str, ChangeState] = {
    "armed": ChangeState.AWAITING_CHECKS,
    "merging": ChangeState.AWAITING_CHECKS,
    "parked": ChangeState.AWAITING_HUMAN,
    "escalated": ChangeState.AWAITING_HUMAN,
    "reaped": ChangeState.REAPED,
    "held": ChangeState.FAILED,
    "rehearsed": ChangeState.FAILED,
    "reverted": ChangeState.FAILED,
    "closed": ChangeState.CLOSED,
}


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    finding_id: str
    state: ChangeState | str
    title: str
    loop: str
    risk: str | None = None
    pr: int | None = None
    head_sha: str | None = None
    detail: str = ""
    branch: str = ""
    partial: bool = False


@dataclass(frozen=True, slots=True)
class FindingProjection:
    finding_id: str
    state: ChangeState
    title: str
    loop: str
    risk: str | None
    pr: int | None
    head_sha: str | None
    detail: str
    branch: str
    partial: bool
    ts: str


def finding_id(loop: str, title: str) -> str:
    normalized = re.sub(r"\s+", " ", title.strip().casefold())
    return hashlib.sha256(f"{loop}\0{normalized}".encode()).hexdigest()[:16]


def candidate_id(finding: str, base_sha: str, patch_digest: str, run_id: str) -> str:
    """Bind a publication identity to one exact analyzed change."""
    material = "\0".join((finding, base_sha, patch_digest, run_id))
    return hashlib.sha256(material.encode()).hexdigest()[:24]


class Ledger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    def append(self, event: LifecycleEvent) -> None:
        row = asdict(event)
        state = _canonical_state(str(row["state"]))
        if state is None:
            raise ValueError(f"unknown Change Lifecycle state {row['state']!r}")
        row["state"] = state.value
        self._write({"ts": _now(), **row})

    def record(self, **fields: Any) -> None:
        """Write one legacy-shaped row while older callers are migrated."""
        status = str(fields.pop("status", ""))
        state = _canonical_state(status)
        if state is not None:
            fields["state"] = state.value
        self._write({"ts": _now(), **fields})

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def projections(self) -> dict[str, FindingProjection]:
        projected: dict[str, FindingProjection] = {}
        for row in self.rows():
            title = str(row.get("title") or "")
            if not title:
                continue
            loop = str(row.get("loop") or "legacy")
            state = _state(row)
            if state is None:
                continue
            identifier = str(row.get("finding_id") or finding_id(loop, title))
            projected[identifier] = FindingProjection(
                finding_id=identifier,
                state=state,
                title=title,
                loop=loop,
                risk=str(row["risk"]) if row.get("risk") is not None else None,
                pr=int(row["pr"]) if row.get("pr") is not None else None,
                head_sha=str(row["head_sha"]) if row.get("head_sha") else None,
                detail=str(row.get("detail") or ""),
                branch=str(row.get("branch") or ""),
                partial=bool(row.get("partial", False)),
                ts=str(row.get("ts") or ""),
            )
        return projected

    def projection(self, identifier: str) -> FindingProjection | None:
        return self.projections().get(identifier)

    def suppressed_titles(self) -> list[str]:
        return [
            f"[{projection.state}/{projection.risk or 'n/a'}] {projection.title}"
            for projection in self.projections().values()
            if projection.state in SUPPRESSED
        ]

    def handled_titles(self) -> list[str]:
        """Compatibility name for audit prompts."""
        return self.suppressed_titles()

    def _write(self, row: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _state(row: dict[str, Any]) -> ChangeState | None:
    return _canonical_state(str(row.get("state") or row.get("status") or ""))


def _canonical_state(raw: str) -> ChangeState | None:
    try:
        return ChangeState(raw)
    except ValueError:
        return LEGACY_STATES.get(raw)


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "SUPPRESSED",
    "FindingProjection",
    "Ledger",
    "LifecycleEvent",
    "LifecycleState",
    "candidate_id",
    "finding_id",
]
