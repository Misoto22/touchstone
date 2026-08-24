from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from touchstone.scheduling import ScheduleError, parse_schedule


@pytest.mark.parametrize(
    ("raw", "systemd", "launchd"),
    [
        ("hourly", "hourly", None),
        ("daily@03:15", "*-*-* 03:15:00", {"Hour": 3, "Minute": 15}),
        (
            "weekly@MON,09:30",
            "Mon *-*-* 09:30:00",
            {"Weekday": 2, "Hour": 9, "Minute": 30},
        ),
        (
            "weekly@SUN,23:59",
            "Sun *-*-* 23:59:00",
            {"Weekday": 1, "Hour": 23, "Minute": 59},
        ),
    ],
)
def test_schedule_has_stable_native_calendars(
    raw: str, systemd: str, launchd: dict[str, int] | None
) -> None:
    schedule = parse_schedule(raw)

    assert schedule.systemd_calendar() == systemd
    assert schedule.launchd_calendar() == launchd


@pytest.mark.parametrize(
    "raw",
    ["", "daily@25:00", "daily@12:60", "weekly@FUNDAY,09:00", "*/5 * * * *"],
)
def test_invalid_or_unsupported_schedules_are_rejected(raw: str) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(raw)


def test_next_after_converts_repository_time_to_utc() -> None:
    schedule = parse_schedule("daily@09:30")

    due = schedule.next_after(
        dt.datetime(2026, 8, 24, 0, tzinfo=dt.UTC), ZoneInfo("Australia/Sydney")
    )

    assert due == dt.datetime(2026, 8, 24, 23, 30, tzinfo=dt.UTC)


def test_nonexistent_dst_time_shifts_forward_and_repeated_time_runs_once() -> None:
    new_york = ZoneInfo("America/New_York")
    spring = parse_schedule("daily@02:30").next_after(
        dt.datetime(2026, 3, 8, 5, tzinfo=dt.UTC), new_york
    )
    assert spring == dt.datetime(2026, 3, 8, 7, 30, tzinfo=dt.UTC)

    schedule = parse_schedule("daily@01:30")
    first = schedule.next_after(dt.datetime(2026, 11, 1, 4, tzinfo=dt.UTC), new_york)
    after_first = schedule.next_after(first, new_york)
    assert first == dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.UTC)
    assert after_first == dt.datetime(2026, 11, 2, 6, 30, tzinfo=dt.UTC)
