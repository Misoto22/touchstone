from __future__ import annotations

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
