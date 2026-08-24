from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

from touchstone.cli import _scheduler
from touchstone.execution.base import Result
from touchstone.execution.local import LocalExecutor
from touchstone.scheduling.base import find_touchstone_executable
from touchstone.scheduling.launchd import LaunchdScheduler
from touchstone.scheduling.systemd import SystemdScheduler


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        source=SimpleNamespace(path=(tmp_path / "touchstone.toml").resolve()),
        repo_path=(tmp_path / "project").resolve(),
        state_dir=(tmp_path / "state").resolve(),
        # The unit's start timeout is derived from this: systemd must not win a
        # race against the timeout that produces a diagnosis.
        engine=SimpleNamespace(timeout_seconds=2700),
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
    assert 'Environment="PATH=' in service
    assert "GH_TOKEN" not in service
    assert "SECRET" not in service


def test_scheduler_dry_run_writes_and_executes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "rendered"
    scheduler = SystemdScheduler(LocalExecutor(), executable=Path("/absolute/bin/touchstone"))

    report = scheduler.install(_config(tmp_path), target=target, dry_run=True)

    assert len(report.files) == 4
    assert not target.exists()


def test_executable_discovery_uses_the_active_python_environment(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    import sys

    binary = tmp_path / "venv" / "bin" / "touchstone"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(binary.parent / "python"))
    monkeypatch.setattr("shutil.which", lambda command: None)

    assert find_touchstone_executable() == binary.resolve()


def test_native_scheduler_commands_always_run_on_the_local_orchestrator(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import touchstone.scheduling

    observed: list[object] = []
    monkeypatch.setattr(
        touchstone.scheduling,
        "current_scheduler",
        lambda executor: observed.append(executor) or executor,
    )

    scheduler = _scheduler(SimpleNamespace())

    assert isinstance(scheduler, LocalExecutor)
    assert observed == [scheduler]


class FailingNativeExecutor:
    def __init__(self, *, print_ok: bool = False) -> None:
        self.print_ok = print_ok

    def run(self, argv, **_kwargs):  # type: ignore[no-untyped-def]
        if argv[:2] == ["launchctl", "print"]:
            return Result(0 if self.print_ok else 1, "", "not loaded")
        return Result(1, "", "native command failed")


def test_launchd_uninstall_refuses_to_hide_a_bootout_failure(tmp_path: Path) -> None:
    scheduler = LaunchdScheduler(
        FailingNativeExecutor(print_ok=True),
        executable=Path("/absolute/bin/touchstone"),
        home=tmp_path,
    )
    scheduler.install(_config(tmp_path), target=tmp_path / "Library" / "LaunchAgents")

    import pytest

    with pytest.raises(RuntimeError, match="disable"):
        scheduler.uninstall(_config(tmp_path))


def test_systemd_uninstall_refuses_to_hide_a_disable_failure(tmp_path: Path) -> None:
    scheduler = SystemdScheduler(
        FailingNativeExecutor(),
        executable=Path("/absolute/bin/touchstone"),
        home=tmp_path,
    )
    scheduler.install(_config(tmp_path), target=tmp_path / ".config" / "systemd" / "user")

    import pytest

    with pytest.raises(RuntimeError, match="disable"):
        scheduler.uninstall(_config(tmp_path))


def test_the_generated_unit_lets_the_engine_give_up_first(tmp_path) -> None:
    """A `oneshot` service inherits `DefaultTimeoutStartUSec`, 90 seconds on a
    stock systemd, while an audit session runs for minutes. Left at the default
    every scheduled run is killed before it finishes — and killed by systemd,
    so no node writes a ledger row and the loop looks like it found nothing.
    """
    from touchstone.scheduling.systemd import SystemdScheduler, _start_timeout

    config = _config(tmp_path)
    rendered = SystemdScheduler(LocalExecutor(), executable=Path("/bin/touchstone"))._render(
        config, tmp_path
    )
    service = next(text for path, text in rendered.items() if path.suffix == ".service")

    timeout = _start_timeout(config)
    assert f"TimeoutStartSec={timeout}\n" in service
    assert timeout > config.engine.timeout_seconds, (
        "systemd would kill the run before the engine reaches its own ceiling, "
        "and a systemd kill records nothing"
    )
    assert "After=network-online.target" in service
