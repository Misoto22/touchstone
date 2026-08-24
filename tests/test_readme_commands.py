from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_documented_first_run_commands_exist() -> None:
    result = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "touchstone"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    for command in ("init", "doctor", "setup", "run", "status", "install-scheduler"):
        assert command in result.stdout


def test_readme_starts_with_the_installable_first_run() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = readme.split("### Getting Started", 1)[1].split("\n---\n", 1)[0]
    commands = [
        "pipx install touchstone-agent",
        "touchstone init",
        "touchstone doctor",
        "touchstone setup",
        "touchstone run code --dry-run",
        "touchstone install-scheduler",
    ]

    positions = [getting_started.index(command) for command in commands]
    assert positions == sorted(positions)


def test_public_policy_files_are_linked_from_the_readme() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for path in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md"):
        assert (ROOT / path).is_file()
        assert f"]({path})" in readme
