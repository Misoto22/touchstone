from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from touchstone.config import ConfigError
from touchstone.discovery import discover_project
from touchstone.execution.local import LocalExecutor


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def make_repo(tmp_path: Path, *, remote: str, branch: str = "trunk") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", branch, str(repo)], check=True, capture_output=True, text=True
    )
    _git(repo, "remote", "add", "origin", remote)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    return repo


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:acme/widgets.git",
        "https://github.com/acme/widgets.git",
        "ssh://git@github.com/acme/widgets.git",
    ],
)
def test_discovers_slug_and_default_branch_from_origin(tmp_path: Path, remote: str) -> None:
    repo = make_repo(tmp_path, remote=remote)

    found = discover_project(repo, LocalExecutor())

    assert found.root == repo.resolve()
    assert found.slug == "acme/widgets"
    assert found.default_branch == "trunk"
    assert found.scheduler in {"launchd", "systemd", "unsupported"}


def test_discovery_refuses_a_non_github_origin(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, remote="https://example.com/acme/widgets.git")

    with pytest.raises(ConfigError, match="GitHub origin"):
        discover_project(repo, LocalExecutor())
