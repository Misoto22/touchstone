from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from touchstone.execution.local import LocalExecutor
from touchstone.validation import ValidationCommand, prepare, run_gate, validate_commands


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.test")
    (tmp_path / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "test")
    return tmp_path


def test_validation_scrubs_secret_like_environment(git_repo: Path) -> None:
    command = ValidationCommand(
        target="root",
        argv=(
            sys.executable,
            "-c",
            "import os; print(','.join(sorted(os.environ)))",
        ),
        enabled=True,
    )

    result = run_gate(
        git_repo,
        Path("."),
        command,
        LocalExecutor(),
        env={"OPENAI_API_KEY": "secret", "PATH": os.environ["PATH"]},
    )

    assert result.ok
    assert "OPENAI_API_KEY" not in result.stdout
    assert "secret" not in result.stdout
    assert "PATH" in result.stdout


def test_tracked_file_mutation_blocks_validation(git_repo: Path) -> None:
    command = ValidationCommand(
        target="root",
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ),
        enabled=True,
    )

    report = validate_commands(git_repo, Path("."), (command,), LocalExecutor())

    assert report.outcome == "blocked"
    assert report.results[0].reason == "tracked-files-changed"


def test_nonzero_timeout_and_disabled_candidates_are_typed(git_repo: Path) -> None:
    commands = (
        ValidationCommand(target="root", argv=(sys.executable, "-c", "pass")),
        ValidationCommand(
            target="root",
            argv=(sys.executable, "-c", "import time; time.sleep(2)"),
            timeout_seconds=1,
            enabled=True,
        ),
    )

    report = validate_commands(git_repo, Path("."), commands, LocalExecutor())

    assert report.results[0].reason == "disabled"
    assert report.results[1].reason == "timeout"
    assert report.outcome == "blocked"


def test_target_working_directory_cannot_escape(git_repo: Path) -> None:
    with pytest.raises(ValueError, match="Target working directory"):
        ValidationCommand(
            target="web",
            argv=(sys.executable, "-c", "pass"),
            cwd=Path("../../outside"),
            enabled=True,
        )


def test_shell_requires_an_explicit_executable_and_risk_acknowledgement(
    git_repo: Path,
) -> None:
    with pytest.raises(ValueError, match="risk acknowledgement"):
        run_gate(
            git_repo,
            Path("."),
            ValidationCommand(
                target="root",
                argv=("sh", "-c", "true"),
                shell=True,
                enabled=True,
            ),
            LocalExecutor(),
        )

    result = run_gate(
        git_repo,
        Path("."),
        ValidationCommand(
            target="root",
            argv=("sh", "-c", "true"),
            shell=True,
            risk_acknowledged=True,
            enabled=True,
        ),
        LocalExecutor(),
    )
    assert result.ok


def test_validation_never_uses_implicit_shell_strings() -> None:
    with pytest.raises(ValueError, match="argv"):
        ValidationCommand(target="root", argv="pytest")  # type: ignore[arg-type]


def test_preparation_uses_each_targets_confirmed_package_manager(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    commands: list[tuple[str, tuple[str, ...]]] = []

    def capture(_root, _path, command, _executor):  # type: ignore[no-untyped-def]
        commands.append((command.target, command.argv))
        return SimpleNamespace(ok=True)

    gate = SimpleNamespace(
        enabled=True,
        preparation="locked-install",
        allow_scripts=False,
        allow_build_hooks=False,
    )
    config = SimpleNamespace(
        repo_path=Path("."),
        targets={
            "web": SimpleNamespace(
                path=Path("apps/web"), validation=(gate,), package_managers=("npm",)
            ),
            "api": SimpleNamespace(
                path=Path("services/api"), validation=(gate,), package_managers=("uv",)
            ),
        },
        generated_metadata=SimpleNamespace(package_managers=("npm", "uv")),
    )
    monkeypatch.setattr("touchstone.validation.run_gate", capture)

    report = prepare(config, ("web", "api"), object())  # type: ignore[arg-type]

    assert report.outcome == "completed"
    assert commands == [
        ("web", ("npm", "ci", "--ignore-scripts")),
        ("api", ("uv", "sync", "--frozen", "--no-install-project")),
    ]
