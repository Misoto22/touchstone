"""macOS per-user launchd adapter."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
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


def _not_loaded(result: Any) -> bool:
    detail = result.tail().casefold()
    return any(marker in detail for marker in ("could not find service", "not loaded"))


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
            active = self._executor.run(["launchctl", "print", f"{domain}/{label}"], timeout=30)
            if active.ok:
                unloaded = self._executor.run(
                    ["launchctl", "bootout", f"{domain}/{label}"], timeout=30
                )
                if not unloaded.ok:
                    raise RuntimeError(f"could not disable {label}: {unloaded.tail()}")
            elif not _not_loaded(active):
                raise RuntimeError(f"could not inspect {label}: {active.tail()}")
            loaded = self._executor.run(["launchctl", "bootstrap", domain, str(path)], timeout=30)
            if not loaded.ok:
                raise RuntimeError(f"could not enable {label}: {loaded.tail()}")
            commands.append(f"launchctl print {domain}/{label}")
        for path in self._legacy_files(config, destination):
            if path in report.files or not path.exists():
                continue
            active = self._executor.run(["launchctl", "print", f"{domain}/{path.stem}"], timeout=30)
            if active.ok:
                unloaded = self._executor.run(
                    ["launchctl", "bootout", f"{domain}/{path.stem}"], timeout=30
                )
                if not unloaded.ok:
                    raise RuntimeError(f"could not disable legacy {path.stem}: {unloaded.tail()}")
            elif not _not_loaded(active):
                raise RuntimeError(f"could not inspect legacy {path.stem}: {active.tail()}")
            path.unlink()
        return replace(report, commands=tuple(commands))

    def uninstall(
        self, config: Any, *, target: Path | None = None, dry_run: bool = False
    ) -> InstallReport:
        destination = (target or self._home / "Library" / "LaunchAgents").resolve()
        owned = self._wake_files(config, destination)
        # Install already boots out and deletes the pre-hash job names. Uninstall did not, so an
        # upgraded installation kept firing from `io.touchstone.agent.wake.plist` after a
        # successful uninstall. Only files this configuration owns are touched.
        legacy = tuple(
            path
            for path in self._legacy_files(config, destination)
            if path.exists() and path not in owned
        )
        files = owned + legacy
        if target is None and not dry_run:
            domain = f"gui/{os.getuid()}"
            for path in files:
                active = self._executor.run(
                    ["launchctl", "print", f"{domain}/{path.stem}"], timeout=30
                )
                if active.ok:
                    unloaded = self._executor.run(
                        ["launchctl", "bootout", f"{domain}/{path.stem}"], timeout=30
                    )
                    if not unloaded.ok:
                        raise RuntimeError(f"could not disable {path.stem}: {unloaded.tail()}")
                elif not _not_loaded(active):
                    raise RuntimeError(f"could not inspect {path.stem}: {active.tail()}")
        changed = tuple(path for path in files if path.exists())
        if not dry_run:
            for path in changed:
                path.unlink()
        return InstallReport(files=files, changed=changed)

    def status(self, config: Any) -> SchedulerStatus:
        destination = self._home / "Library" / "LaunchAgents"
        owned = self._wake_files(config, destination)
        expected = tuple(self._render(config, destination))
        return SchedulerStatus(
            adapter=self.name,
            supported=True,
            installed=tuple(path for path in owned if path.exists()),
            missing=tuple(path for path in expected if not path.exists()),
        )

    def _render(self, config: Any, destination: Path) -> dict[Path, str]:
        if not any(loop.schedule for loop in config.loops.values()):
            return {}
        label = self._label(config)
        payload: dict[str, Any] = {
            "Label": label,
            "ProgramArguments": [
                str(self._executable),
                "--config",
                str(Path(config.source.path).resolve()),
                "run-due",
            ],
            "WorkingDirectory": str(Path(config.repo_path).resolve()),
            "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": command_path(self._executable)},
            "StandardOutPath": str(Path(config.state_dir).resolve() / "logs" / "wake.log"),
            "StandardErrorPath": str(Path(config.state_dir).resolve() / "logs" / "wake.error.log"),
            "StartInterval": 900,
        }
        content = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode()
        return {destination / f"{label}.plist": content}

    def _label(self, config: Any) -> str:
        slug = str(getattr(getattr(config, "forge", None), "slug", "repository"))
        normalized = re.sub(r"[^a-z0-9]+", "-", slug.casefold()).strip("-") or "repository"
        identity = str(Path(config.source.path).resolve())
        digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
        return f"io.touchstone.agent.{normalized}-{digest}.wake"

    def _wake_files(self, config: Any, destination: Path) -> tuple[Path, ...]:
        """The agent path this adapter owns, whether or not a schedule exists."""
        return (destination / f"{self._label(config)}.plist",)

    def _legacy_files(self, config: Any, destination: Path) -> tuple[Path, ...]:
        candidates = [destination / "io.touchstone.agent.wake.plist"]
        candidates.extend(
            destination / f"io.touchstone.agent.{name}.plist"
            for name, loop in sorted(config.loops.items())
            if loop.schedule
        )
        expected_config = str(Path(config.source.path).resolve())
        return tuple(
            path
            for path in candidates
            if not path.exists() or self._legacy_selects(path, expected_config)
        )

    @staticmethod
    def _legacy_selects(path: Path, expected_config: str) -> bool:
        try:
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            return False
        arguments = payload.get("ProgramArguments", [])
        return isinstance(arguments, list) and expected_config in arguments


__all__ = ["LaunchdScheduler"]
