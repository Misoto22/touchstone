"""The deliberately small schedule language shared by every adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

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
        f"unsupported schedule {raw!r}; use hourly@MM, daily@HH:MM, "
        "or weekly@DAY,HH:MM"
    )


def _clock(hour_raw: str, minute_raw: str, original: str) -> tuple[int, int]:
    hour = int(hour_raw)
    minute = int(minute_raw)
    if hour > 23 or minute > 59:
        raise ScheduleError(f"invalid local time in schedule {original!r}")
    return hour, minute


__all__ = ["Schedule", "ScheduleError", "parse_schedule"]
