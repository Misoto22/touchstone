from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_discovery import make_repo
from touchstone.cli import main
from touchstone.config import ConfigError, load_config
from touchstone.discovery import discover_project
from touchstone.execution.local import LocalExecutor
from touchstone.initialize import InitOptions, initialize


def test_non_interactive_init_writes_a_loadable_generic_config(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, remote="https://github.com/acme/widgets.git")
    options = InitOptions(
        start=repo,
        engine="codex",
        model="gpt-test",
        workflows=("ci.yml",),
        schedule="hourly",
    )

    path = initialize(options, LocalExecutor())
    config = load_config(path)

    assert path == repo / "touchstone.toml"
    assert config.repo_path == repo.resolve()
    assert config.forge.slug == "acme/widgets"
    assert config.forge.default_branch == "trunk"
    assert config.loop("code").schedule == "hourly"
    text = path.read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "api_key" not in text.lower()


def test_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, remote="git@github.com:acme/widgets.git")
    target = repo / "touchstone.toml"
    target.write_text("keep me", encoding="utf-8")
    options = InitOptions(start=repo, engine="codex", model="gpt-test")

    with pytest.raises(ConfigError, match="already exists"):
        initialize(options, LocalExecutor())

    assert target.read_text(encoding="utf-8") == "keep me"


def test_discovery_result_can_be_reused_by_init(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, remote="git@github.com:acme/widgets.git")
    executor = LocalExecutor()
    discovered = discover_project(repo, executor)

    path = initialize(
        InitOptions(start=repo, engine="claude", model="claude-test", discovered=discovered),
        executor,
    )

    assert 'name = "claude"' in path.read_text(encoding="utf-8")


def test_non_interactive_init_is_available_from_the_cli(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, remote="git@github.com:acme/widgets.git")

    code = main(
        [
            "init",
            "--path",
            str(repo),
            "--non-interactive",
            "--engine",
            "codex",
            "--model",
            "gpt-test",
            "--workflow",
            "ci.yml",
            "--schedule",
            "hourly",
        ]
    )

    assert code == 0
    assert load_config(repo / "touchstone.toml").forge.slug == "acme/widgets"


def test_non_interactive_init_requires_an_explicit_workflow(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, remote="git@github.com:acme/widgets.git")

    code = main(
        [
            "init",
            "--path",
            str(repo),
            "--non-interactive",
            "--engine",
            "codex",
            "--model",
            "gpt-test",
        ]
    )

    assert code == 78
    assert not (repo / "touchstone.toml").exists()


def test_non_interactive_init_requires_an_explicit_schedule(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, remote="git@github.com:acme/widgets.git")

    code = main(
        [
            "init",
            "--path",
            str(repo),
            "--non-interactive",
            "--engine",
            "codex",
            "--model",
            "gpt-test",
            "--workflow",
            "ci.yml",
        ]
    )

    assert code == 78
    assert not (repo / "touchstone.toml").exists()
