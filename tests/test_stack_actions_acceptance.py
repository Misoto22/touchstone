from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "acceptance"


@dataclass(frozen=True, slots=True)
class WheelEnv:
    executable: Path
    env: dict[str, str]

    def run(self, *argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.executable), *argv],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


@pytest.fixture(scope="module")
def wheel_env(tmp_path_factory: pytest.TempPathFactory) -> WheelEnv:
    root = tmp_path_factory.mktemp("installed-wheel")
    dist = root / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    environment = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / "bin" / "python"
    wheel = next(dist.glob("touchstone_agent-*.whl"))
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheel)],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    clean = os.environ.copy()
    clean.pop("PYTHONPATH", None)
    clean["XDG_STATE_HOME"] = str(root / "state")
    return WheelEnv(environment / "bin" / "touchstone", clean)


def _repository(tmp_path: Path, fixture: str) -> Path:
    repository = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, repository)
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            f"https://github.com/acme/{fixture}.git",
        ],
        check=True,
    )
    return repository


@pytest.mark.parametrize(
    ("fixture", "profiles"),
    [
        ("next-app", {"nextjs", "react", "typescript", "javascript", "node"}),
        ("django-app", {"django", "python"}),
        ("mixed-monorepo", {"nextjs", "react", "typescript", "django", "python"}),
    ],
)
def test_installed_wheel_initializes_detected_repository(
    wheel_env: WheelEnv,
    tmp_path: Path,
    fixture: str,
    profiles: set[str],
) -> None:
    repository = _repository(tmp_path, fixture)

    initialized = wheel_env.run(
        "init",
        "--non-interactive",
        "--engine",
        "codex",
        "--model",
        "gpt-test",
        "--workflow",
        "ci.yml",
        "--schedule",
        "hourly@00",
        cwd=repository,
    )
    detected = wheel_env.run("profile", "detect", "--json", cwd=repository)

    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    assert detected.returncode == 0, detected.stdout + detected.stderr
    payload = json.loads(detected.stdout)
    actual = {profile for target in payload["targets"] for profile in target["profiles"]}
    assert profiles <= actual
    assert (repository / "touchstone.toml").is_file()
    assert (repository / ".touchstone" / "generated.toml").is_file()
    assert wheel_env.run("profile", "refresh", "--check", cwd=repository).returncode == 0


def test_installed_wheel_renders_and_checks_a_safe_actions_workflow(
    wheel_env: WheelEnv, tmp_path: Path
) -> None:
    repository = _repository(tmp_path, "next-app")
    initialized = wheel_env.run(
        "init",
        "--non-interactive",
        "--engine",
        "codex",
        "--model",
        "gpt-test",
        "--workflow",
        "ci.yml",
        "--schedule",
        "hourly@00",
        cwd=repository,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    sha = "a" * 40

    generated = wheel_env.run("actions", "init", "--action-sha", sha, cwd=repository)
    checked = wheel_env.run("actions", "init", "--action-sha", sha, "--check", cwd=repository)
    workflow = (repository / ".github" / "workflows" / "touchstone.yml").read_text(encoding="utf-8")

    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "pull_request:" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert (
        "TOUCHSTONE_APP_PRIVATE_KEY"
        not in workflow.split("  analysis:", 1)[1].split("  publish:", 1)[0]
    )


def test_composite_entrypoint_executes_a_real_hosted_prepare_stage(
    wheel_env: WheelEnv, tmp_path: Path
) -> None:
    repository = _repository(tmp_path, "next-app")
    initialized = wheel_env.run(
        "init",
        "--non-interactive",
        "--engine",
        "codex",
        "--model",
        "gpt-test",
        "--workflow",
        "ci.yml",
        "--schedule",
        "hourly@00",
        cwd=repository,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr

    runner_temp = tmp_path / "runner"
    action_ref = "a" * 40
    action_venv = runner_temp / "touchstone-action-12345-1"
    shutil.copytree(wheel_env.executable.parents[1], action_venv, symlinks=True)
    (action_venv / ".touchstone-action-ref").write_text(action_ref + "\n", encoding="utf-8")
    environment = wheel_env.env | {
        "GITHUB_ACTION_PATH": str(ROOT),
        "GITHUB_ACTION_REF": action_ref,
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
        "RUNNER_TEMP": str(runner_temp),
    }
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TOUCHSTONE_STATE_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        environment.pop(name, None)

    installed = subprocess.run(
        [str(ROOT / "scripts" / "action-entrypoint.sh"), "install"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    prepared = subprocess.run(
        [str(ROOT / "scripts" / "action-entrypoint.sh"), "prepare"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    payload = json.loads(
        (repository / ".touchstone" / "hosted" / "prepare" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["stage"] == "prepare"
    assert payload["outcome"] == "completed"
