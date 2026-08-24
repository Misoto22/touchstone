from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.config import ConfigError, discover_config_path, load_config


def _valid_config(*, project_path: str = ".", brief: str = "builtin:code-audit") -> str:
    return f'''\
version = 1

[project]
path = "{project_path}"

[forge]
slug = "acme/widgets"

[engine]
name = "codex"
model = "gpt-test"

[loop.code]
brief = "{brief}"
label = "touchstone:audit"

[loop.code.context]
project = "this repository"
ledger = "the project ledger"
protected = "the configured protected paths"
rules_clause = ""
'''


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_relative_paths_are_resolved_from_the_config_file(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "nested" / "touchstone.toml",
        _valid_config(project_path="../repo"),
    )

    loaded = load_config(config_path)

    assert loaded.repo_path == (tmp_path / "repo").resolve()
    assert loaded.source.path == config_path.resolve()
    assert loaded.source.schema_version == 1


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    raw = _valid_config().replace('model = "gpt-test"', 'model = "gpt-test"\nmodle = "wrong"')
    path = _write(tmp_path / "touchstone.toml", raw)

    with pytest.raises(ConfigError, match=r"engine\.modle"):
        load_config(path)


def test_builtin_brief_is_available_from_package_resources(tmp_path: Path) -> None:
    path = _write(tmp_path / "touchstone.toml", _valid_config())

    prompt = load_config(path).loop("code").prompt()

    assert "Take the queue before you search" in prompt


def test_unversioned_config_requires_migration(tmp_path: Path) -> None:
    path = _write(tmp_path / "touchstone.toml", _valid_config().replace("version = 1\n", ""))

    with pytest.raises(ConfigError, match=r"touchstone config migrate"):
        load_config(path)


def test_discovery_honours_xdg_config_home(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    start = tmp_path / "work"
    start.mkdir()
    xdg = tmp_path / "xdg"
    expected = _write(xdg / "touchstone" / "config.toml", _valid_config())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("TOUCHSTONE_CONFIG", raising=False)

    assert discover_config_path(start) == expected


def test_discovery_falls_back_to_home_config_after_custom_xdg(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    start = tmp_path / "work"
    start.mkdir()
    home = tmp_path / "home"
    expected = _write(home / ".config" / "touchstone" / "config.toml", _valid_config())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-xdg"))
    monkeypatch.setattr("touchstone.config.Path.home", classmethod(lambda _cls: home))

    assert discover_config_path(start) == expected


def test_ssh_runtime_paths_come_from_the_ssh_section(tmp_path: Path) -> None:
    raw = (
        _valid_config()
        + """

[execution]
target = "ssh"

[execution.ssh]
host = "audit.example"
workdir = "/srv/widgets"
state_dir = "/var/lib/touchstone"
"""
    )
    config = load_config(_write(tmp_path / "touchstone.toml", raw))

    assert config.execution_repo == "/srv/widgets"
    assert config.execution_worktree == "/var/lib/touchstone/worktree"


def test_ssh_runtime_paths_must_be_absolute(tmp_path: Path) -> None:
    raw = (
        _valid_config()
        + """

[execution]
target = "ssh"

[execution.ssh]
host = "audit.example"
workdir = "relative/repository"
state_dir = "/var/lib/touchstone"
"""
    )

    with pytest.raises(ConfigError, match="absolute"):
        load_config(_write(tmp_path / "touchstone.toml", raw))


def test_config_rejects_secret_shaped_ssh_environment_keys(tmp_path: Path) -> None:
    raw = (
        _valid_config()
        + """

[execution]
target = "ssh"

[execution.ssh]
host = "audit.example"
workdir = "/srv/widgets"
state_dir = "/var/lib/touchstone"
env = { GH_TOKEN = "must-not-live-in-toml" }
"""
    )

    with pytest.raises(ConfigError, match="secret-like"):
        load_config(_write(tmp_path / "touchstone.toml", raw))


def test_default_state_directories_are_isolated_per_repository(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    first = load_config(_write(tmp_path / "one.toml", _valid_config()))
    second = load_config(
        _write(
            tmp_path / "two.toml",
            _valid_config().replace('slug = "acme/widgets"', 'slug = "acme/gadgets"'),
        )
    )

    assert first.state_dir.parent == tmp_path / "state" / "touchstone"
    assert second.state_dir.parent == tmp_path / "state" / "touchstone"
    assert first.state_dir != second.state_dir


@pytest.mark.parametrize(
    ("needle", "replacement", "message"),
    [
        ('slug = "acme/widgets"', 'slug = "acme/widgets"\nrequired_workflows = "ci.yml"', "array"),
        ('model = "gpt-test"', 'model = "gpt-test"\ntimeout_seconds = 0', "positive"),
        (
            'label = "touchstone:audit"',
            'label = "touchstone:audit"\nprotected_paths = ".github/"',
            "array",
        ),
        ('label = "touchstone:audit"', 'label = ""', "must not be empty"),
    ],
)
def test_config_rejects_invalid_types_and_ranges(
    tmp_path: Path, needle: str, replacement: str, message: str
) -> None:
    raw = _valid_config().replace(needle, replacement)

    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path / "touchstone.toml", raw))
