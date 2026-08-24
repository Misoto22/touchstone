"""Portable schedules and native user-timer adapters."""

from __future__ import annotations

import sys

from touchstone.execution import Executor
from touchstone.scheduling.base import InstallReport, Scheduler, SchedulerStatus
from touchstone.scheduling.model import Schedule, ScheduleError, parse_schedule


def build_scheduler(platform: str, executor: Executor) -> Scheduler:
    if platform == "darwin":
        from touchstone.scheduling.launchd import LaunchdScheduler

        return LaunchdScheduler(executor)
    if platform.startswith("linux"):
        from touchstone.scheduling.systemd import SystemdScheduler

        return SystemdScheduler(executor)
    raise RuntimeError(f"native scheduling is unsupported on {platform}")


def current_scheduler(executor: Executor) -> Scheduler:
    return build_scheduler(sys.platform, executor)


__all__ = [
    "InstallReport",
    "Schedule",
    "ScheduleError",
    "Scheduler",
    "SchedulerStatus",
    "build_scheduler",
    "current_scheduler",
    "parse_schedule",
]
