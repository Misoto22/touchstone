"""When an unattended merge is allowed to be armed.

A Loop's schedule says when it runs; a merge window says when what it produced
may reach the default branch without a person. The two are different questions
and a project answers them differently: running an audit at three in the
morning is fine, and merging its output at three in the morning — with nobody
awake to notice a bad one for six hours — is a separate decision.

The window bounds when Touchstone *arms* the merge, not when the forge performs
it. `gh pr merge --auto` hands the merge to GitHub, which completes it once the
required checks pass, and that can be minutes later. A window is therefore a
statement about when unattended merging may start, not a guarantee about when
it finishes; a project that needs the harder guarantee wants a required check
that fails outside its window, which is the forge's job rather than this one's.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from touchstone.scheduling.model import _WEEKDAYS

_WINDOW = re.compile(
    r"^(?P<first>[A-Z]{3})(?:-(?P<last>[A-Z]{3}))?,"
    r"(?P<open_hour>\d{2}):(?P<open_minute>\d{2})-"
    r"(?P<close_hour>\d{2}):(?P<close_minute>\d{2})$"
)


class WindowError(ValueError):
    """The merge window is unusable, and no merge may be armed against it."""


@dataclass(frozen=True, slots=True)
class MergeWindow:
    """One span of the week, in the configuration's own timezone."""

    #: Weekday indices this window covers, Monday being 0.
    days: tuple[int, ...]
    opens: tuple[int, int]
    closes: tuple[int, int]

    def covers(self, moment: dt.datetime) -> bool:
        """Whether a local moment falls inside this window.

        The closing edge is exclusive, so a window written as `09:00-17:00`
        means what a person reading "until five" means.
        """

        if moment.weekday() not in self.days:
            return False
        clock = (moment.hour, moment.minute)
        return self.opens <= clock < self.closes


def parse_windows(raw: tuple[str, ...]) -> tuple[MergeWindow, ...]:
    """Read `DAY[-DAY],HH:MM-HH:MM` entries, refusing anything ambiguous."""

    return tuple(_parse_one(entry) for entry in raw)


def within_windows(windows: tuple[MergeWindow, ...], moment: dt.datetime, timezone: str) -> bool:
    """Whether `moment` falls inside any window.

    No window at all means no restriction, which keeps a Loop that never
    configured one behaving exactly as it did before windows existed.
    """

    if not windows:
        return True
    local = moment.astimezone(ZoneInfo(timezone))
    return any(window.covers(local) for window in windows)


def _parse_one(raw: str) -> MergeWindow:
    match = _WINDOW.match(raw.strip())
    if match is None:
        raise WindowError(
            f"merge window {raw!r} must read DAY,HH:MM-HH:MM or DAY-DAY,HH:MM-HH:MM, "
            "with three-letter days such as MON"
        )
    first = _weekday(match["first"], raw)
    last = _weekday(match["last"], raw) if match["last"] else first
    opens = _clock(match["open_hour"], match["open_minute"], raw)
    closes = _clock(match["close_hour"], match["close_minute"], raw)
    if opens >= closes:
        # Two windows say it unambiguously. One that wraps invites a reader to
        # guess whether 23:00-02:00 means three hours or twenty-one, and the
        # reader deciding wrong is a merge at the hour the project banned.
        raise WindowError(
            f"merge window {raw!r} crosses midnight or is empty; write two windows instead"
        )
    if first <= last:
        days = tuple(range(first, last + 1))
    else:
        # SAT-SUN is a range of two; FRI-MON wraps the week and is the same
        # ambiguity as a wrapping clock, so it is refused rather than guessed.
        raise WindowError(
            f"merge window {raw!r} runs backwards through the week; write two windows instead"
        )
    return MergeWindow(days=days, opens=opens, closes=closes)


def _weekday(name: str, raw: str) -> int:
    try:
        return _WEEKDAYS[name][0]
    except KeyError:
        known = ", ".join(_WEEKDAYS)
        raise WindowError(f"merge window {raw!r} names day {name!r}; use one of {known}") from None


def _clock(hour_raw: str, minute_raw: str, raw: str) -> tuple[int, int]:
    hour, minute = int(hour_raw), int(minute_raw)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise WindowError(f"merge window {raw!r} names an impossible time of day")
    return hour, minute


__all__ = ["MergeWindow", "WindowError", "parse_windows", "within_windows"]
