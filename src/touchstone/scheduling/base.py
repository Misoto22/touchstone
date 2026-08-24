"""Shared scheduler reports and deterministic file writes."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class InstallReport:
    files: tuple[Path, ...]
    changed: tuple[Path, ...] = ()
    commands: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    adapter: str
    supported: bool
    installed: tuple[Path, ...] = ()
    missing: tuple[Path, ...] = ()
    detail: str = ""


class Scheduler(Protocol):
    def install(
        self, config: Any, *, target: Path | None = None, dry_run: bool = False
    ) -> InstallReport: ...

    def uninstall(
        self, config: Any, *, target: Path | None = None, dry_run: bool = False
    ) -> InstallReport: ...

    def status(self, config: Any) -> SchedulerStatus: ...


def write_files(rendered: dict[Path, str], *, dry_run: bool) -> InstallReport:
    changed = tuple(
        path
        for path, content in rendered.items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    )
    if not dry_run:
        for path, content in rendered.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return InstallReport(files=tuple(rendered), changed=changed)


def command_path(executable: Path) -> str:
    """A credential-free PATH containing Touchstone and its declared CLIs."""
    directories = [str(executable.resolve().parent)]
    for command in ("git", "gh", "codex", "claude", "ssh"):
        found = shutil.which(command)
        if found:
            directories.append(str(Path(found).resolve().parent))
    for conventional in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"):
        if Path(conventional).is_dir():
            directories.append(conventional)
    return os.pathsep.join(dict.fromkeys(directories))


def find_touchstone_executable() -> Path:
    found = shutil.which("touchstone")
    if found:
        return Path(found).resolve()
    beside_python = Path(sys.executable).parent / "touchstone"
    if beside_python.is_file():
        return beside_python.resolve()
    raise RuntimeError("could not locate the touchstone executable")


__all__ = [
    "InstallReport",
    "Scheduler",
    "SchedulerStatus",
    "command_path",
    "find_touchstone_executable",
    "write_files",
]
