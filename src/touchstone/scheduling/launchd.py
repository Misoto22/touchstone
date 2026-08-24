"""macOS per-user launchd adapter."""

from __future__ import annotations

import os
import plistlib
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


class LaunchdScheduler:
    name = "launchd"

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
        destination = (target or self._home / "Library" / "LaunchAgents").resolve()
        rendered = self._render(config, destination)
        report = write_files(rendered, dry_run=dry_run)
        if target is not None or dry_run:
            return report
        (Path(config.state_dir).resolve() / "logs").mkdir(parents=True, exist_ok=True)
        commands: list[str] = []
        domain = f"gui/{os.getuid()}"
        for path in report.files:
            label = path.stem
            self._executor.run(["launchctl", "bootout", f"{domain}/{label}"], timeout=30)
            loaded = self._executor.run(["launchctl", "bootstrap", domain, str(path)], timeout=30)
            if not loaded.ok:
                raise RuntimeError(f"could not enable {label}: {loaded.tail()}")
            commands.append(f"launchctl print {domain}/{label}")
        return replace(report, commands=tuple(commands))

    def uninstall(
        self, config: Any, *, target: Path | None = None, dry_run: bool = False
    ) -> InstallReport:
        destination = (target or self._home / "Library" / "LaunchAgents").resolve()
        files = tuple(self._render(config, destination))
        if target is None and not dry_run:
            domain = f"gui/{os.getuid()}"
            for path in files:
                self._executor.run(["launchctl", "bootout", f"{domain}/{path.stem}"], timeout=30)
        changed = tuple(path for path in files if path.exists())
        if not dry_run:
            for path in changed:
                path.unlink()
        return InstallReport(files=files, changed=changed)

    def status(self, config: Any) -> SchedulerStatus:
        destination = self._home / "Library" / "LaunchAgents"
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
            label = f"io.touchstone.agent.{name}"
            schedule = parse_schedule(loop.schedule)
            payload: dict[str, Any] = {
                "Label": label,
                "ProgramArguments": [
                    str(self._executable),
                    "--config",
                    str(Path(config.source.path).resolve()),
                    "run",
                    name,
                ],
                "WorkingDirectory": str(Path(config.repo_path).resolve()),
                "ProcessType": "Background",
                "EnvironmentVariables": {"PATH": command_path(self._executable)},
                "StandardOutPath": str(Path(config.state_dir).resolve() / "logs" / f"{name}.log"),
                "StandardErrorPath": str(
                    Path(config.state_dir).resolve() / "logs" / f"{name}.error.log"
                ),
            }
            calendar = schedule.launchd_calendar()
            if calendar is None:
                payload["StartInterval"] = 3600
            else:
                payload["StartCalendarInterval"] = calendar
            content = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode()
            rendered[destination / f"{label}.plist"] = content
        return rendered


__all__ = ["LaunchdScheduler"]
