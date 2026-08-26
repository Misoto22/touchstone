"""The container entrypoint: one repository, one clock.

Kept separate from `execution.container`, which holds the supervisor loop and
has no opinion about where its configuration comes from. This module is the
part that reads the environment, and it is deliberately small: everything it
does is already a supported command.
"""

from __future__ import annotations

import contextlib
import os
import sys

from touchstone.execution.container import MINIMUM_INTERVAL_SECONDS, supervise


def _interval() -> int:
    raw = os.environ.get("TOUCHSTONE_CONTAINER_INTERVAL_SECONDS", "900")
    try:
        seconds = int(raw)
    except ValueError:
        raise SystemExit(
            f"TOUCHSTONE_CONTAINER_INTERVAL_SECONDS must be a whole number of seconds, not {raw!r}"
        ) from None
    if seconds < MINIMUM_INTERVAL_SECONDS:
        raise SystemExit(
            f"TOUCHSTONE_CONTAINER_INTERVAL_SECONDS must be at least "
            f"{MINIMUM_INTERVAL_SECONDS}, not {seconds}"
        )
    return seconds


def main() -> int:
    from touchstone.cli import main as cli

    # Python block-buffers stdout when it is not a tty, which is exactly what a
    # container gets. Left alone, `run-due` prints what it did into a buffer
    # nobody sees until it fills - the log stays empty while the loop works.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(line_buffering=True)

    interval = _interval()
    print(f"touchstone: waking run-due every {interval}s", flush=True)
    report = supervise(run=lambda: cli(["run-due"]), interval_seconds=interval)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
