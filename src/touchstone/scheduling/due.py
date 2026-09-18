"""Schedule generations and coalesced Due Slot evaluation."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from touchstone.scheduling.model import parse_schedule
from touchstone.scheduling.store import DueStore


@dataclass(frozen=True, slots=True)
class DueSlot:
    loop_id: str
    generation: str
    scheduled_for_utc: dt.datetime
    manual: bool = False

    def __post_init__(self) -> None:
        if self.scheduled_for_utc.tzinfo is None:
            raise ValueError("Due Slot time must be timezone-aware")

    @property
    def id(self) -> str:
        payload = (
            f"{self.loop_id}\0{self.generation}\0"
            f"{self.scheduled_for_utc.astimezone(dt.UTC).isoformat()}\0{int(self.manual)}"
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DueLoop:
    slot: DueSlot
    missed_count: int
    lateness: dt.timedelta
    priority: int
    clean_start: bool = False


def schedule_generation(
    loop_id: str,
    schedule: str,
    timezone: str,
    *,
    policy: dict[str, Any] | None = None,
) -> str:
    parsed = parse_schedule(schedule)
    payload = {
        "loop": loop_id,
        "schedule": parsed.normalized,
        "timezone": timezone,
        "policy": policy or {"attempts": 3},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


class DueEvaluator:
    def __init__(self, store: DueStore) -> None:
        self._store = store

    def evaluate(self, config: Any, now: dt.datetime) -> tuple[DueLoop, ...]:
        if now.tzinfo is None:
            raise ValueError("Due evaluation requires an aware datetime")
        current = now.astimezone(dt.UTC).replace(microsecond=0)
        try:
            timezone = ZoneInfo(config.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown IANA timezone {config.timezone!r}") from None
        due: list[DueLoop] = []
        for loop_id, loop in config.loops.items():
            if not loop.schedule:
                continue
            schedule = parse_schedule(loop.schedule)
            generation = schedule_generation(loop_id, loop.schedule, config.timezone)
            pending = self._store.retry_due(loop_id, generation, current)
            if pending is not None:
                slot = DueSlot(loop_id, generation, pending.scheduled_for_utc)
                due.append(
                    DueLoop(
                        slot,
                        pending.missed_count,
                        max(dt.timedelta(), current - slot.scheduled_for_utc),
                        getattr(loop, "priority", 100),
                    )
                )
                continue

            initial, watermark = self._store.generation_state(loop_id, generation, current)
            if watermark is None:
                slot = DueSlot(loop_id, generation, initial)
                due.append(
                    DueLoop(
                        slot,
                        0,
                        max(dt.timedelta(), current - initial),
                        getattr(loop, "priority", 100),
                        clean_start=True,
                    )
                )
                continue

            candidate = schedule.next_after(watermark, timezone)
            if candidate > current:
                continue
            latest = candidate
            count = 1
            while True:
                following = schedule.next_after(latest, timezone)
                if following > current:
                    break
                latest = following
                count += 1
                if count > 100_000:
                    raise RuntimeError("schedule catch-up exceeded safety bound")
            due.append(
                DueLoop(
                    DueSlot(loop_id, generation, latest),
                    count,
                    current - latest,
                    getattr(loop, "priority", 100),
                )
            )
        # Priority is the operator's word and still wins outright. Within one
        # priority, the Loop that has waited longest goes first: a hosted run
        # claims a single slot, so ordering equals alone by loop id handed
        # every wake to whichever name sorted first and starved the rest of
        # a shared schedule indefinitely, however long they had been due.
        return tuple(
            sorted(
                due,
                key=lambda item: (item.priority, -item.lateness, item.slot.loop_id),
            )
        )


__all__ = ["DueEvaluator", "DueLoop", "DueSlot", "schedule_generation"]
