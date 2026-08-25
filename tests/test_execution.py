"""What an executor does when the command is not there to run."""

from __future__ import annotations

from pathlib import Path

from touchstone.execution.local import LocalExecutor


def test_a_missing_command_is_an_exit_code_not_a_traceback() -> None:
    result = LocalExecutor().run(["touchstone-no-such-command", "--version"], timeout=30)

    assert result.code == 127
    assert not result.ok
    assert not result.timed_out
    assert "touchstone-no-such-command" in result.stderr


def test_a_command_that_cannot_be_executed_reports_the_shell_code(tmp_path: Path) -> None:
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o600)

    result = LocalExecutor().run([str(script)], timeout=30)

    assert result.code == 126
    assert not result.ok


def test_a_missing_working_directory_is_an_exit_code(tmp_path: Path) -> None:
    result = LocalExecutor().run(["git", "status"], cwd=str(tmp_path / "gone"), timeout=30)

    assert result.code == 127
    assert not result.ok
