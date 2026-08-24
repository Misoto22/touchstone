from __future__ import annotations

import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path


def test_distribution_uses_public_name_and_contains_briefs() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["name"] == "touchstone-agent"
    assert files("touchstone.resources").joinpath("briefs", "code-audit.md").is_file()


def test_generated_langgraph_state_is_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", ".langgraph_api"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert tracked == ""
    assert ".langgraph_api/" in Path(".gitignore").read_text(encoding="utf-8")
