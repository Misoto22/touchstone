from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_documented_first_run_commands_exist() -> None:
    result = subprocess.run(
        [str(Path(sys.executable).parent / "touchstone"), "--help"],
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
    ]

    positions = [getting_started.index(command) for command in commands]
    assert positions == sorted(positions)
    assert getting_started.count("touchstone doctor") == 2
    assert "touchstone install-scheduler" not in getting_started


def test_readme_links_the_published_release() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]

    assert "https://pypi.org/project/touchstone-agent/" in readme
    assert f"https://github.com/Misoto22/touchstone/releases/tag/v{version}" in readme
    assert "release candidate" not in readme
    assert "Before the first PyPI release" not in readme


def test_readme_resources_work_outside_github() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"]\(([^)\s]+)\)", readme)
    targets.extend(re.findall(r'\b(?:src|srcset)="([^"]+)"', readme))
    relative_targets = sorted(
        target
        for target in targets
        if not target.startswith(("https://", "http://", "#", "mailto:"))
    )

    assert relative_targets == []


def test_public_policy_files_are_linked_from_the_readme() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for path in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md"):
        assert (ROOT / path).is_file()
        assert f"](https://github.com/Misoto22/touchstone/blob/main/{path})" in readme
