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
        ("api", ("uv", "sync", "--frozen", "--no-install-workspace", "--no-build")),
    ]


def test_polyglot_target_runs_one_locked_preparation_per_ecosystem(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    commands: list[tuple[str, ...]] = []

    def capture(_root, _path, command, _executor):  # type: ignore[no-untyped-def]
        commands.append(command.argv)
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
            "app": SimpleNamespace(
                path=Path("."),
                validation=(gate,),
                package_managers=("npm", "uv"),
            )
        },
    )
    monkeypatch.setattr("touchstone.validation.run_gate", capture)

    report = prepare(config, ("app",), object())  # type: ignore[arg-type]

    assert report.outcome == "completed"
    assert commands == [
        ("npm", "ci", "--ignore-scripts"),
        ("uv", "sync", "--frozen", "--no-install-workspace", "--no-build"),
    ]


def _prepared(
    monkeypatch,  # type: ignore[no-untyped-def]
    repository: Path,
    manager: str,
    *,
    target_path: Path = Path("."),
    allow_scripts: bool = False,
    allow_build_hooks: bool = False,
):  # type: ignore[no-untyped-def]
    """Capture the locked preparation Touchstone would run for one manager."""
    from types import SimpleNamespace

    captured: list[ValidationCommand] = []

    def capture(_root, _path, command, _executor):  # type: ignore[no-untyped-def]
        captured.append(command)
        return SimpleNamespace(ok=True, reason="passed", argv=command.argv)

    gate = SimpleNamespace(
        enabled=True,
        preparation="locked-install",
        allow_scripts=allow_scripts,
        allow_build_hooks=allow_build_hooks,
    )
    config = SimpleNamespace(
        repo_path=repository,
        targets={
            "app": SimpleNamespace(
                path=target_path,
                validation=(gate,),
                package_managers=(manager,),
            )
        },
    )
    monkeypatch.setattr("touchstone.validation.run_gate", capture)
    report = prepare(config, ("app",), object())  # type: ignore[arg-type]
    return report, captured


def test_bun_preparation_is_frozen_and_hook_free(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _report, commands = _prepared(monkeypatch, tmp_path, "bun")

    assert commands[0].argv == ("bun", "install", "--frozen-lockfile", "--ignore-scripts")
    assert commands[0].extra_env == ()


def test_yarn_classic_preparation_uses_frozen_lockfile(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")

    _report, commands = _prepared(monkeypatch, tmp_path, "yarn")

    assert commands[0].argv == ("yarn", "install", "--frozen-lockfile", "--ignore-scripts")


def test_yarn_modern_is_detected_from_yarnrc(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".yarnrc.yml").write_text("nodeLinker: node-modules\n", encoding="utf-8")

    _report, commands = _prepared(monkeypatch, tmp_path, "yarn")

    assert commands[0].argv == ("yarn", "install", "--immutable", "--mode=skip-build")


def test_yarn_modern_is_detected_from_declared_package_manager(  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "apps" / "web"
    target.mkdir(parents=True)
    (target / "package.json").write_text('{"packageManager": "yarn@4.5.0"}', encoding="utf-8")

    _report, commands = _prepared(
        monkeypatch,
        tmp_path,
        "yarn",
        target_path=Path("apps/web"),
    )

    assert commands[0].argv == ("yarn", "install", "--immutable", "--mode=skip-build")


def test_uv_preparation_installs_no_workspace_member_and_builds_nothing(  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    _report, commands = _prepared(monkeypatch, tmp_path, "uv")

    assert commands[0].argv == (
        "uv",
        "sync",
        "--frozen",
        "--no-install-workspace",
        "--no-build",
    )


def test_pdm_preparation_forces_binary_only_resolution(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _report, commands = _prepared(monkeypatch, tmp_path, "pdm")

    assert commands[0].argv == ("pdm", "sync", "--frozen-lockfile", "--no-self")
    assert commands[0].extra_env == (("PDM_ONLY_BINARY", ":all:"),)


def test_pdm_preparation_environment_reaches_the_subprocess(git_repo: Path) -> None:
    command = ValidationCommand(
        target="root",
        argv=(sys.executable, "-c", "import os; print(os.environ.get('PDM_ONLY_BINARY', ''))"),
        enabled=True,
        extra_env=(("PDM_ONLY_BINARY", ":all:"),),
    )

    result = run_gate(
        git_repo, Path("."), command, LocalExecutor(), env={"PATH": os.environ["PATH"]}
    )

    assert result.ok
    assert result.stdout.strip() == ":all:"


def test_hook_free_poetry_preparation_is_structured_not_a_crash(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    report, commands = _prepared(monkeypatch, tmp_path, "poetry")

    assert commands == []
    assert report.outcome == "blocked"
    assert report.results[0].reason == "policy-unsupported"
    assert report.results[0].argv == ("poetry", "install")


def test_poetry_preparation_is_permitted_once_build_hooks_are_accepted(  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
) -> None:
    report, commands = _prepared(monkeypatch, tmp_path, "poetry", allow_build_hooks=True)

    assert report.outcome == "completed"
    assert commands[0].argv == ("poetry", "install")


def test_missing_package_manager_evidence_blocks_without_raising(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    report, commands = _prepared(monkeypatch, tmp_path, "")

    assert commands == []
    assert report.outcome == "blocked"
    assert report.results[0].reason == "policy-unsupported"
