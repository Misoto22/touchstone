"""The deliberately small schedule language shared by every adapter."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

_WEEKDAYS = {
    "MON": (0, "Mon", 2),
    "TUE": (1, "Tue", 3),
    "WED": (2, "Wed", 4),
    "THU": (3, "Thu", 5),
    "FRI": (4, "Fri", 6),
    "SAT": (5, "Sat", 7),
    "SUN": (6, "Sun", 1),
}
_HOURLY = re.compile(r"hourly@(\d{2})")
_DAILY = re.compile(r"daily@(\d{2}):(\d{2})")
_WEEKLY = re.compile(r"weekly@([A-Z]{3}),(\d{2}):(\d{2})")


class ScheduleError(ValueError):
    """A schedule cannot be represented consistently on supported hosts."""


@dataclass(frozen=True, slots=True)
class Schedule:
    frequency: Literal["hourly", "daily", "weekly"]
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None

    @property
    def normalized(self) -> str:
        minute = self.minute or 0
        if self.frequency == "hourly":
            return f"hourly@{minute:02d}"
        assert self.hour is not None
        if self.frequency == "daily":
            return f"daily@{self.hour:02d}:{minute:02d}"
        assert self.weekday is not None
        weekday = next(name for name, values in _WEEKDAYS.items() if values[0] == self.weekday)
        return f"weekly@{weekday},{self.hour:02d}:{minute:02d}"

    def next_after(self, instant: dt.datetime, timezone: ZoneInfo) -> dt.datetime:
        if instant.tzinfo is None:
            raise ScheduleError("next_after requires an aware datetime")
        utc_instant = instant.astimezone(dt.UTC)
        local_now = utc_instant.astimezone(timezone)
        naive_now = local_now.replace(tzinfo=None)
        minute = self.minute or 0
        if self.frequency == "hourly":
            candidate = naive_now.replace(minute=minute, second=0, microsecond=0)
            if candidate <= naive_now:
                candidate += dt.timedelta(hours=1)
            step = dt.timedelta(hours=1)
        elif self.frequency == "daily":
            assert self.hour is not None
            candidate = naive_now.replace(hour=self.hour, minute=minute, second=0, microsecond=0)
            if candidate <= naive_now:
                candidate += dt.timedelta(days=1)
            step = dt.timedelta(days=1)
        else:
            assert self.hour is not None and self.weekday is not None
            days = (self.weekday - naive_now.weekday()) % 7
            candidate = (naive_now + dt.timedelta(days=days)).replace(
                hour=self.hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= naive_now:
                candidate += dt.timedelta(days=7)
            step = dt.timedelta(days=7)

        for _ in range(4):
            resolved = _resolve_local(candidate, timezone)
            if resolved > utc_instant:
                return resolved
            candidate += step
        raise ScheduleError("could not resolve the next schedule occurrence")

    def systemd_calendar(self) -> str:
        if self.frequency == "hourly":
            return "hourly" if self.minute is None else f"*-*-* *:{self.minute:02d}:00"
        assert self.hour is not None and self.minute is not None
        clock = f"{self.hour:02d}:{self.minute:02d}:00"
        if self.frequency == "daily":
            return f"*-*-* {clock}"
        assert self.weekday is not None
        weekday = next(values[1] for values in _WEEKDAYS.values() if values[0] == self.weekday)
        return f"{weekday} *-*-* {clock}"

    def launchd_calendar(self) -> dict[str, int] | None:
        if self.frequency == "hourly":
            return None if self.minute is None else {"Minute": self.minute}
        assert self.hour is not None and self.minute is not None
        calendar = {"Hour": self.hour, "Minute": self.minute}
        if self.frequency == "weekly":
            assert self.weekday is not None
            calendar["Weekday"] = next(
                values[2] for values in _WEEKDAYS.values() if values[0] == self.weekday
            )
        return calendar


def parse_schedule(raw: str) -> Schedule:
    if raw == "hourly":
        return Schedule("hourly")
    hourly = _HOURLY.fullmatch(raw)
    if hourly:
        minute = int(hourly.group(1))
        if minute > 59:
            raise ScheduleError(f"invalid local time in schedule {raw!r}")
        return Schedule("hourly", minute=minute)
    daily = _DAILY.fullmatch(raw)
    if daily:
        hour, minute = _clock(daily.group(1), daily.group(2), raw)
        return Schedule("daily", hour=hour, minute=minute)
    weekly = _WEEKLY.fullmatch(raw)
    if weekly:
        weekday = _WEEKDAYS.get(weekly.group(1))
        if weekday is None:
            raise ScheduleError(f"unsupported weekday in schedule {raw!r}")
        hour, minute = _clock(weekly.group(2), weekly.group(3), raw)
        return Schedule("weekly", hour=hour, minute=minute, weekday=weekday[0])
    raise ScheduleError(
        f"unsupported schedule {raw!r}; use hourly@MM, daily@HH:MM, or weekly@DAY,HH:MM"
    )


def _clock(hour_raw: str, minute_raw: str, original: str) -> tuple[int, int]:
    hour = int(hour_raw)
    minute = int(minute_raw)
    if hour > 23 or minute > 59:
        raise ScheduleError(f"invalid local time in schedule {original!r}")
    return hour, minute


def _resolve_local(candidate: dt.datetime, timezone: ZoneInfo) -> dt.datetime:
    local = candidate.replace(tzinfo=timezone, fold=0)
    utc = local.astimezone(dt.UTC)
    round_trip = utc.astimezone(timezone)
    if round_trip.replace(tzinfo=None) != candidate:
        return round_trip.astimezone(dt.UTC)
    return utc


__all__ = ["Schedule", "ScheduleError", "parse_schedule"]
