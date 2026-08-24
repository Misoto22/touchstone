from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

from touchstone.execution.local import LocalExecutor
from touchstone.scheduling.launchd import LaunchdScheduler
from touchstone.scheduling.systemd import SystemdScheduler


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        source=SimpleNamespace(path=(tmp_path / "touchstone.toml").resolve()),
        repo_path=(tmp_path / "project").resolve(),
        state_dir=(tmp_path / "state").resolve(),
        loops={
            "code": SimpleNamespace(name="code", schedule="hourly"),
            "weekly": SimpleNamespace(name="weekly", schedule="weekly@MON,09:30"),
            "manual": SimpleNamespace(name="manual", schedule=None),
        },
    )


def test_launchd_file_has_absolute_paths_and_no_environment_secrets(tmp_path: Path) -> None:
    target = tmp_path / "rendered"
    scheduler = LaunchdScheduler(LocalExecutor(), executable=Path("/absolute/bin/touchstone"))

    report = scheduler.install(_config(tmp_path), target=target)

    assert len(report.files) == 2
    with report.files[0].open("rb") as handle:
        plist = plistlib.load(handle)
    arguments = plist["ProgramArguments"]
    assert arguments[0] == "/absolute/bin/touchstone"
    assert str((tmp_path / "touchstone.toml").resolve()) in arguments
    text = report.files[0].read_text(encoding="utf-8")
    assert "GH_TOKEN" not in text
    assert "SECRET" not in text


def test_systemd_install_is_idempotent_and_skips_unscheduled_loops(tmp_path: Path) -> None:
    target = tmp_path / "rendered"
    scheduler = SystemdScheduler(LocalExecutor(), executable=Path("/absolute/bin/touchstone"))

    first = scheduler.install(_config(tmp_path), target=target)
    second = scheduler.install(_config(tmp_path), target=target)

    assert len(first.files) == 4
    assert first.files == second.files
    assert second.changed == ()
    service = (target / "touchstone-code.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=" + str((tmp_path / "project").resolve()) in service
    assert "Environment=" not in service


def test_scheduler_dry_run_writes_and_executes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "rendered"
    scheduler = SystemdScheduler(LocalExecutor(), executable=Path("/absolute/bin/touchstone"))

    report = scheduler.install(_config(tmp_path), target=target, dry_run=True)

    assert len(report.files) == 4
    assert not target.exists()
