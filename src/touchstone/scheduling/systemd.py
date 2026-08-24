"""Linux systemd user-timer adapter."""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any

from touchstone.execution import Executor
from touchstone.scheduling.base import (
    InstallReport,
    SchedulerStatus,
    command_path,
    find_touchstone_executable,
    write_files,
)
from touchstone.scheduling.model import parse_schedule


def _start_timeout(config: Any) -> int:
    """Longer than the engine's own ceiling, deliberately.

    `DefaultTimeoutStartUSec` is 90 seconds on a stock systemd, and a `oneshot`
    service is held to it. An audit session runs for minutes — seven, measured,
    on the loop this was written for — so without this every scheduled run is
    killed before it finishes, on every host, at every interval.

    It fails the worst way available: systemd kills the process, so no node
    writes a ledger row and no verdict is recorded. The evidence is a service
    that ran and a queue that never moves, which reads as a loop finding
    nothing rather than a loop being stopped.

    So the engine has to give up first, and its ceiling is what the run reports
    when it does. A run makes up to two engine calls — the audit and the
    independent review — hence twice `timeout_seconds`, plus room for the git
    and forge work between them. systemd stays a last resort against a wedged
    process, not a competitor to the timeout that produces a diagnosis.
    """
    return 2 * int(config.engine.timeout_seconds) + 600


class SystemdScheduler:
    name = "systemd"

    def __init__(
        self,
        executor: Executor,
        *,
        executable: Path | None = None,
        home: Path | None = None,
    ) -> None:
        self._executor = executor
        self._executable = (executable or find_touchstone_executable()).resolve()
        self._home = (home or Path.home()).resolve()

    def install(
        self, config: Any, *, target: Path | None = None, dry_run: bool = False
    ) -> InstallReport:
        destination = (target or self._home / ".config" / "systemd" / "user").resolve()
        report = write_files(self._render(config, destination), dry_run=dry_run)
        if target is not None or dry_run:
            return report
        reloaded = self._executor.run(["systemctl", "--user", "daemon-reload"], timeout=30)
        if not reloaded.ok:
            raise RuntimeError(f"could not reload systemd: {reloaded.tail()}")
        timers = [path.name for path in report.files if path.suffix == ".timer"]
        enabled = self._executor.run(
            ["systemctl", "--user", "enable", "--now", *timers], timeout=60
        )
        if not enabled.ok:
            raise RuntimeError(f"could not enable systemd timers: {enabled.tail()}")
        commands = tuple(f"systemctl --user status {timer}" for timer in timers)
        return replace(report, commands=commands)

    def uninstall(
        self, config: Any, *, target: Path | None = None, dry_run: bool = False
    ) -> InstallReport:
        destination = (target or self._home / ".config" / "systemd" / "user").resolve()
        files = tuple(self._render(config, destination))
        timers = [path.name for path in files if path.suffix == ".timer"]
        if target is None and not dry_run and timers:
            disabled = self._executor.run(
                ["systemctl", "--user", "disable", "--now", *timers], timeout=60
            )
            if not disabled.ok:
                raise RuntimeError(f"could not disable systemd timers: {disabled.tail()}")
        changed = tuple(path for path in files if path.exists())
        if not dry_run:
            for path in changed:
                path.unlink()
            if target is None:
                reloaded = self._executor.run(["systemctl", "--user", "daemon-reload"], timeout=30)
                if not reloaded.ok:
                    raise RuntimeError(f"could not reload systemd: {reloaded.tail()}")
        return InstallReport(files=files, changed=changed)

    def status(self, config: Any) -> SchedulerStatus:
        destination = self._home / ".config" / "systemd" / "user"
        files = tuple(self._render(config, destination))
        return SchedulerStatus(
            adapter=self.name,
            supported=True,
            installed=tuple(path for path in files if path.exists()),
            missing=tuple(path for path in files if not path.exists()),
        )

    def _render(self, config: Any, destination: Path) -> dict[Path, str]:
        rendered: dict[Path, str] = {}
        for name, loop in sorted(config.loops.items()):
            if not loop.schedule:
                continue
            unit = f"touchstone-{name}"
            command = shlex.join(
                [
                    str(self._executable),
                    "--config",
                    str(Path(config.source.path).resolve()),
                    "run",
                    name,
                ]
            )
            service = (
                "[Unit]\n"
                f"Description=Touchstone loop: {name}\n"
                # A run needs the forge and the engine. Not a promise the
                # network works, only that the job does not start before it
                # could.
                "After=network-online.target\n"
                "Wants=network-online.target\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"WorkingDirectory={Path(config.repo_path).resolve()}\n"
                f'Environment="PATH={command_path(self._executable)}"\n'
                f"TimeoutStartSec={_start_timeout(config)}\n"
                f"ExecStart={command}\n"
            )
            calendar = parse_schedule(loop.schedule).systemd_calendar()
            timer = (
                "[Unit]\n"
                f"Description=Schedule Touchstone loop: {name}\n\n"
                "[Timer]\n"
                f"OnCalendar={calendar}\n"
                "Persistent=true\n"
                f"Unit={unit}.service\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n"
            )
            rendered[destination / f"{unit}.service"] = service
            rendered[destination / f"{unit}.timer"] = timer
        return rendered


__all__ = ["SystemdScheduler"]
