"""Transactional SQLite storage for schedule generations and durable claims."""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from touchstone.outcomes import RunOutcome, RunResult


@dataclass(frozen=True, slots=True)
class DurableClaim:
    slot_id: str
    owner: str
    expires_at: dt.datetime
    attempt: int


@dataclass(frozen=True, slots=True)
class ClaimResult:
    acquired: bool
    claim: DurableClaim | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SlotRecord:
    slot_id: str
    loop_id: str
    generation: str
    scheduled_for_utc: dt.datetime
    attempts: int
    outcome: str
    consumed_at: dt.datetime | None
    next_retry_at: dt.datetime | None
    claim_owner: str
    claim_expires_at: dt.datetime | None
    missed_count: int
    partial: bool


class DueStore:
    def __init__(self, path: Path, *, max_attempts: int = 3) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS generations (
                    loop_id TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    initial_scheduled TEXT NOT NULL,
                    last_scheduled TEXT,
                    PRIMARY KEY (loop_id, generation)
                );
                CREATE TABLE IF NOT EXISTS slots (
                    slot_id TEXT PRIMARY KEY,
                    loop_id TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    missed_count INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL DEFAULT '',
                    consumed_at TEXT,
                    next_retry_at TEXT,
                    claim_owner TEXT NOT NULL DEFAULT '',
                    claim_expires_at TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    snapshot TEXT NOT NULL DEFAULT '',
                    partial INTEGER NOT NULL DEFAULT 0
                );
                PRAGMA user_version=1;
                """
            )

    def generation_state(
        self, loop_id: str, generation: str, now: dt.datetime
    ) -> tuple[dt.datetime, dt.datetime | None]:
        instant = _utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO generations "
                "(loop_id, generation, initial_scheduled) VALUES (?, ?, ?)",
                (loop_id, generation, _format(instant)),
            )
            row = connection.execute(
                "SELECT initial_scheduled, last_scheduled FROM generations "
                "WHERE loop_id = ? AND generation = ?",
                (loop_id, generation),
            ).fetchone()
            connection.commit()
        assert row is not None
        return _parse(row[0]), _parse(row[1]) if row[1] else None

    def claim(
        self,
        slot: object,
        *,
        owner: str,
        now: dt.datetime,
        ttl: dt.timedelta,
        missed_count: int = 0,
    ) -> ClaimResult:
        from touchstone.scheduling.due import DueSlot

        if not isinstance(slot, DueSlot):
            raise TypeError("claim requires a DueSlot")
        current = _utc(now)
        expires = current + ttl
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts, consumed_at, next_retry_at, claim_owner, claim_expires_at "
                "FROM slots WHERE slot_id = ?",
                (slot.id,),
            ).fetchone()
            repository_holder = connection.execute(
                "SELECT slot_id FROM slots WHERE slot_id != ? AND claim_expires_at > ? "
                "ORDER BY claim_expires_at DESC LIMIT 1",
                (slot.id, _format(current)),
            ).fetchone()
            if repository_holder is not None:
                connection.rollback()
                return ClaimResult(False, reason="repository-claimed")
            if row is not None:
                attempts, consumed, retry, holder, claim_expiry = row
                if consumed:
                    connection.rollback()
                    return ClaimResult(False, reason="consumed")
                if retry and _parse(retry) > current:
                    connection.rollback()
                    return ClaimResult(False, reason="retry-not-due")
                if claim_expiry and _parse(claim_expiry) > current:
                    if holder == owner:
                        connection.rollback()
                        return ClaimResult(
                            True,
                            DurableClaim(slot.id, owner, _parse(claim_expiry), attempts),
                        )
                    connection.rollback()
                    return ClaimResult(False, reason="claimed")
                attempt = int(attempts) + 1
                connection.execute(
                    "UPDATE slots SET attempts = ?, claim_owner = ?, claim_expires_at = ?, "
                    "next_retry_at = NULL WHERE slot_id = ?",
                    (attempt, owner, _format(expires), slot.id),
                )
            else:
                attempt = 1
                connection.execute(
                    "INSERT INTO slots "
                    "(slot_id, loop_id, generation, scheduled_for, missed_count, attempts, "
                    "claim_owner, claim_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        slot.id,
                        slot.loop_id,
                        slot.generation,
                        _format(slot.scheduled_for_utc),
                        missed_count,
                        attempt,
                        owner,
                        _format(expires),
                    ),
                )
            connection.commit()
        return ClaimResult(True, DurableClaim(slot.id, owner, expires, attempt))

    def finish(
        self,
        claim: DurableClaim,
        result: RunResult,
        *,
        now: dt.datetime,
        snapshot: str = "",
    ) -> None:
        current = _utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT loop_id, generation, scheduled_for, attempts, claim_owner "
                "FROM slots WHERE slot_id = ?",
                (claim.slot_id,),
            ).fetchone()
            if row is None or row[4] != claim.owner:
                connection.rollback()
                raise ValueError("Durable Claim is missing or owned by another worker")
            loop_id, generation, scheduled, attempts, _owner = row
            terminal_failure = result.outcome == RunOutcome.FAILED and attempts >= self.max_attempts
            consumed = result.outcome != RunOutcome.FAILED or terminal_failure or result.partial
            retry_at = None
            if not consumed:
                retry_at = current + dt.timedelta(minutes=5 * (2 ** (attempts - 1)))
            connection.execute(
                "UPDATE slots SET outcome = ?, consumed_at = ?, next_retry_at = ?, "
                "claim_owner = '', claim_expires_at = NULL, reason = ?, snapshot = ?, "
                "partial = ? WHERE slot_id = ?",
                (
                    result.outcome.value,
                    _format(current) if consumed else None,
                    _format(retry_at) if retry_at else None,
                    result.reason_code,
                    snapshot,
                    int(result.partial),
                    claim.slot_id,
                ),
            )
            if consumed:
                connection.execute(
                    "UPDATE generations SET last_scheduled = ? "
                    "WHERE loop_id = ? AND generation = ?",
                    (scheduled, loop_id, generation),
                )
            connection.commit()

    def retry_due(self, loop_id: str, generation: str, now: dt.datetime) -> SlotRecord | None:
        current = _utc(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM slots WHERE loop_id = ? AND generation = ? "
                "AND consumed_at IS NULL AND outcome = ? AND partial = 0 "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY scheduled_for LIMIT 1",
                (loop_id, generation, RunOutcome.FAILED.value, _format(current)),
            ).fetchone()
        return _record(row) if row else None

    def record(self, slot_id: str) -> SlotRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
        return _record(row) if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def _record(row: sqlite3.Row) -> SlotRecord:
    return SlotRecord(
        slot_id=row["slot_id"],
        loop_id=row["loop_id"],
        generation=row["generation"],
        scheduled_for_utc=_parse(row["scheduled_for"]),
        attempts=int(row["attempts"]),
        outcome=row["outcome"],
        consumed_at=_parse(row["consumed_at"]) if row["consumed_at"] else None,
        next_retry_at=_parse(row["next_retry_at"]) if row["next_retry_at"] else None,
        claim_owner=row["claim_owner"],
        claim_expires_at=(_parse(row["claim_expires_at"]) if row["claim_expires_at"] else None),
        missed_count=int(row["missed_count"]),
        partial=bool(row["partial"]),
    )


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("Due Store datetimes must be aware")
    return value.astimezone(dt.UTC).replace(microsecond=0)


def _format(value: dt.datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value).astimezone(dt.UTC)


__all__ = ["ClaimResult", "DueStore", "DurableClaim", "SlotRecord"]
