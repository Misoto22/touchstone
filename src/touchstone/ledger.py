"""Append-only finding lifecycle events and their current projections."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

LifecycleState = Literal[
    "proposed",
    "armed",
    "parked",
    "merged",
    "failed",
    "reaped",
    "closed",
    "held",
    "rehearsed",
]

SUPPRESSED = frozenset({"armed", "parked", "merged"})
LEGACY_STATES: dict[str, LifecycleState] = {
    "merging": "armed",
    "escalated": "parked",
    "reaped": "reaped",
    "held": "held",
    "rehearsed": "rehearsed",
    "reverted": "failed",
    "closed": "closed",
}


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    finding_id: str
    state: LifecycleState
    title: str
    loop: str
    risk: str | None = None
    pr: int | None = None
    head_sha: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FindingProjection:
    finding_id: str
    state: LifecycleState
    title: str
    loop: str
    risk: str | None
    pr: int | None
    head_sha: str | None
    detail: str
    ts: str


def finding_id(loop: str, title: str) -> str:
    normalized = re.sub(r"\s+", " ", title.strip().casefold())
    return hashlib.sha256(f"{loop}\0{normalized}".encode()).hexdigest()[:16]


class Ledger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    def append(self, event: LifecycleEvent) -> None:
        self._write({"ts": _now(), **asdict(event)})

    def record(self, **fields: Any) -> None:
        """Write one legacy-shaped row while older callers are migrated."""
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


def _state(row: dict[str, Any]) -> LifecycleState | None:
    raw = row.get("state")
    if raw in {
        "proposed",
        "armed",
        "parked",
        "merged",
        "failed",
        "reaped",
        "closed",
        "held",
        "rehearsed",
    }:
        return raw
    return LEGACY_STATES.get(str(row.get("status") or ""))


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "SUPPRESSED",
    "FindingProjection",
    "Ledger",
    "LifecycleEvent",
    "LifecycleState",
    "finding_id",
]
