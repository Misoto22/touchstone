"""Portable schedules and native user-timer adapters."""

from __future__ import annotations

import sys

from touchstone.execution import Executor
from touchstone.scheduling.base import InstallReport, Scheduler, SchedulerStatus
from touchstone.scheduling.due import DueEvaluator, DueLoop, DueSlot, schedule_generation
from touchstone.scheduling.model import Schedule, ScheduleError, parse_schedule
from touchstone.scheduling.store import ClaimResult, DueStore, DurableClaim, SlotRecord


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
    "ClaimResult",
    "DueEvaluator",
    "DueLoop",
    "DueSlot",
    "DueStore",
    "DurableClaim",
    "InstallReport",
    "Schedule",
    "ScheduleError",
    "Scheduler",
    "SchedulerStatus",
    "SlotRecord",
    "build_scheduler",
    "current_scheduler",
    "parse_schedule",
    "schedule_generation",
]
