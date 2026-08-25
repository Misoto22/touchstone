"""Loops draw their engine from a named pool instead of one global setting."""

from __future__ import annotations

from pathlib import Path

import pytest

from touchstone.config import ConfigError, load


def _write(path: Path, body: str) -> Path:
    path.write_text(
        "\n".join(
            [
                "version = 1",
                "[project]",
                'path = "."',
                "[forge]",
                'slug = "acme/widgets"',
                body,
            ]
        ),
        encoding="utf-8",
    )
    return path


_DEFAULT_ENGINE = """
[engine]
name = "codex"
model = "codex-default"
"""

_CODE_LOOP = """
[loop.code]
brief = "builtin:code-audit"
label = "touchstone:audit"
"""


def test_the_unnamed_engine_is_the_pool_member_named_default(tmp_path: Path) -> None:
    config = load(_write(tmp_path / "touchstone.toml", _DEFAULT_ENGINE + _CODE_LOOP))

    assert set(config.engines) == {"default"}
    assert config.engines["default"] is config.engine
    assert config.engine_for("code").model == "codex-default"


def test_a_loop_draws_from_the_member_it_names(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path / "touchstone.toml",
            _DEFAULT_ENGINE
            + """
[engine.cheap]
name = "claude"
model = "haiku-test"

[loop.hardcode]
brief = "builtin:code-audit"
label = "touchstone:hardcode"
engine = "cheap"
"""
            + _CODE_LOOP,
        )
    )

    assert set(config.engines) == {"default", "cheap"}
    assert config.engine_for("hardcode").name == "claude"
    assert config.engine_for("hardcode").model == "haiku-test"
    assert config.engine_for("code").name == "codex"


def test_a_loop_model_narrows_the_engine_it_named(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path / "touchstone.toml",
            _DEFAULT_ENGINE
            + """
[engine.strong]
name = "claude"
model = "opus-test"

[loop.naming]
brief = "builtin:code-audit"
label = "touchstone:naming"
engine = "strong"
model = "sonnet-test"
""",
        )
    )

    resolved = config.engine_for("naming")
    assert resolved.name == "claude"
    assert resolved.model == "sonnet-test"
    assert config.engines["strong"].model == "opus-test"


def test_a_loop_naming_an_unknown_engine_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"loop\.code\.engine"):
        load(
            _write(
                tmp_path / "touchstone.toml",
                _DEFAULT_ENGINE
                + """
[loop.code]
brief = "builtin:code-audit"
label = "touchstone:audit"
engine = "absent"
""",
            )
        )


def test_a_member_may_not_reuse_the_reserved_default_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"engine\.default"):
        load(
            _write(
                tmp_path / "touchstone.toml",
                _DEFAULT_ENGINE
                + """
[engine.default]
name = "claude"
model = "m"
"""
                + _CODE_LOOP,
            )
        )


def test_the_budget_subtable_is_not_mistaken_for_a_member(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path / "touchstone.toml",
            _DEFAULT_ENGINE
            + """
[engine.budget]
audit = 5.0
review = 1.0
"""
            + _CODE_LOOP,
        )
    )

    assert set(config.engines) == {"default"}
    assert config.engine.budget.audit == 5.0


def test_a_member_is_validated_like_the_unnamed_engine(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"engine\.cheap\.base_url"):
        load(
            _write(
                tmp_path / "touchstone.toml",
                _DEFAULT_ENGINE
                + """
[engine.cheap]
name = "claude"
model = "m"
base_url = "http://example.test"
"""
                + _CODE_LOOP,
            )
        )


def test_the_key_variable_follows_the_engine_unless_it_is_named(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path / "touchstone.toml",
            _DEFAULT_ENGINE
            + """
[engine.vendor]
name = "claude"
model = "m"

[engine.gateway]
name = "claude"
model = "m"
base_url = "https://gateway.example.test"
api_key_env = "GATEWAY_API_KEY"
"""
            + _CODE_LOOP,
        )
    )

    assert config.engine.key_env == "OPENAI_API_KEY"
    assert config.engines["vendor"].key_env == "ANTHROPIC_API_KEY"
    assert config.engines["gateway"].key_env == "GATEWAY_API_KEY"


def test_a_credential_reference_may_only_be_a_reference(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"engine\.cheap\.api_key_ref"):
        load(
            _write(
                tmp_path / "touchstone.toml",
                _DEFAULT_ENGINE
                + """
[engine.cheap]
name = "claude"
model = "m"
api_key_ref = "sk-live-not-a-reference"
"""
                + _CODE_LOOP,
            )
        )


def test_a_credential_reference_is_kept_for_the_operator(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path / "touchstone.toml",
            _DEFAULT_ENGINE
            + """
[engine.cheap]
name = "claude"
model = "m"
api_key_ref = "op://01 Personal Development/anthropic/credential"
"""
            + _CODE_LOOP,
        )
    )

    assert config.engines["cheap"].api_key_ref.startswith("op://")


def test_a_key_variable_may_not_be_named_after_a_secret_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"engine\.cheap\.api_key_env"):
        load(
            _write(
                tmp_path / "touchstone.toml",
                _DEFAULT_ENGINE
                + """
[engine.cheap]
name = "claude"
model = "m"
api_key_env = "sk-ant-api03-actual-value"
"""
                + _CODE_LOOP,
            )
        )


def test_a_gateway_key_reaches_the_session_under_the_vendor_variable() -> None:
    from touchstone.engines.base import engine_environment

    environment = engine_environment(
        "claude", {"GATEWAY_API_KEY": "value"}, api_key_env="GATEWAY_API_KEY"
    )

    assert environment["ANTHROPIC_API_KEY"] == "value"
    # The session sees the credential it knows how to use and nothing else; the
    # variable the operator stored it under is Touchstone's business.
    assert "GATEWAY_API_KEY" not in environment


def test_a_named_key_that_is_absent_never_falls_back_to_the_vendor_one() -> None:
    from touchstone.engines.base import engine_environment

    environment = engine_environment(
        "claude",
        {"ANTHROPIC_API_KEY": "vendor-value"},
        api_key_env="GATEWAY_API_KEY",
    )

    # Falling back would send the vendor's credential to the gateway endpoint
    # the member configured, which is a credential leak, not a convenience.
    assert "ANTHROPIC_API_KEY" not in environment


def test_a_loop_runs_on_the_engine_it_named(tmp_path: Path) -> None:
    from touchstone.execution.local import LocalExecutor
    from touchstone.nodes.context import Context

    config = load(
        _write(
            tmp_path / "touchstone.toml",
            _DEFAULT_ENGINE
            + """
[engine.reviewer]
name = "claude"
model = "opus-test"

[loop.naming]
brief = "builtin:code-audit"
label = "touchstone:naming"
engine = "reviewer"
"""
            + _CODE_LOOP,
        )
    )
    context = Context.build(config)

    assert context.engine_for("naming").name == "claude"
    assert context.engine_for("code").name == "codex"
    assert context.engine.name == "codex"
    assert isinstance(context.executor, LocalExecutor)


def test_the_workflow_maps_every_configured_engine_key(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from tests.test_actions_workflow import _config
    from touchstone.hosted.workflow import ActionPins, render_workflow

    config = _config(tmp_path)
    config.engines = {
        "default": config.engine,
        "gateway": SimpleNamespace(name="claude", key_env="GATEWAY_API_KEY"),
    }

    rendered = render_workflow(config, ActionPins(), action_sha="a" * 40)

    assert "secrets.GATEWAY_API_KEY" in rendered
