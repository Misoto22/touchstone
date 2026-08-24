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
        legacy = tuple(
            path
            for path in self._legacy_files(config, destination)
            if path not in report.files and path.exists()
        )
        legacy_timers = [path.name for path in legacy if path.suffix == ".timer"]
        if legacy_timers:
            disabled = self._executor.run(
                ["systemctl", "--user", "disable", "--now", *legacy_timers], timeout=60
            )
            if not disabled.ok:
                raise RuntimeError(f"could not disable legacy systemd timers: {disabled.tail()}")
            for path in legacy:
                path.unlink()
            reloaded = self._executor.run(["systemctl", "--user", "daemon-reload"], timeout=30)
            if not reloaded.ok:
                raise RuntimeError(f"could not reload systemd: {reloaded.tail()}")
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
        if not any(loop.schedule for loop in config.loops.values()):
            return {}
        unit = "touchstone-wake"
        command = shlex.join(
            [
                str(self._executable),
                "--config",
                str(Path(config.source.path).resolve()),
                "run-due",
            ]
        )
        service = (
            "[Unit]\n"
            "Description=Wake due Touchstone loops\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"WorkingDirectory={Path(config.repo_path).resolve()}\n"
            f'Environment="PATH={command_path(self._executable)}"\n'
            f"ExecStart={command}\n"
        )
        timer = (
            "[Unit]\n"
            "Description=Wake Touchstone every 15 minutes\n\n"
            "[Timer]\n"
            "OnCalendar=*-*-* *:0/15:00\n"
            "Persistent=true\n"
            f"Unit={unit}.service\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        return {
            destination / f"{unit}.service": service,
            destination / f"{unit}.timer": timer,
        }

    def _legacy_files(self, config: Any, destination: Path) -> tuple[Path, ...]:
        files = []
        for name, loop in sorted(config.loops.items()):
            if loop.schedule:
                files.extend(
                    (
                        destination / f"touchstone-{name}.service",
                        destination / f"touchstone-{name}.timer",
                    )
                )
        return tuple(files)


__all__ = ["SystemdScheduler"]
