"""Driving `run-due` from inside a container, with one clock.

A container is a deployment shape, not a way of running commands: Touchstone
runs inside it and the repository it audits is the only one it can see. One
repository per container, one state volume per container, one credential set
per container — the same isolation the hosted backend gets from separate
runners, obtained here from the container boundary rather than from a namespace
inside a shared process.

`run-due` already claims its own Due Slot, coalesces missed periods, and is
idempotent, so the supervisor is a fixed-interval wake signal and nothing more.
Layering cron on top of it would add a second clock that can drift from the
schedules Touchstone already evaluates, and a missed cron tick would look
exactly like a run that found nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

#: Below this, a wake signal costs more than the schedule it evaluates.
MINIMUM_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class SupervisorReport:
    woken: int = 0
    failed: int = 0

    @property
    def exit_code(self) -> int:
        """Always zero while the supervisor is still supervising.

        A failed wake is a failed run, not a failed supervisor. Exiting on one
        would stop the container and need a person to restart it, which is the
        opposite of what a scheduled backend is for. The count is reported so a
        supervisor that never succeeds is still visible to whoever reads it.
        """

        return 0


def supervise(
    *,
    run: Callable[[], int],
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: int,
    iterations: int | None = None,
) -> SupervisorReport:
    """Wake `run` on a fixed interval until told to stop.

    `iterations` bounds the loop for tests; a container passes nothing and runs
    until the container does not.
    """

    if interval_seconds < MINIMUM_INTERVAL_SECONDS:
        raise ValueError(
            f"container interval must be at least {MINIMUM_INTERVAL_SECONDS} seconds, "
            f"not {interval_seconds}"
        )
    woken = 0
    failed = 0
    while iterations is None or woken < iterations:
        try:
            code = run()
        except Exception:
            failed += 1
        else:
            if code not in (0, 3):
                failed += 1
        woken += 1
        sleep(float(interval_seconds))
    return SupervisorReport(woken=woken, failed=failed)


__all__ = ["MINIMUM_INTERVAL_SECONDS", "SupervisorReport", "supervise"]
