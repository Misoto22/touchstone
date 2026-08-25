from __future__ import annotations

from types import SimpleNamespace

import pytest

from touchstone.config import ConfigError, _model_endpoint
from touchstone.engines.base import engine_environment
from touchstone.engines.codex import CodexEngine


def _engine(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "name": "codex",
        "model": "a-model",
        "audit_effort": "low",
        "review_effort": "low",
        "timeout_seconds": 30,
        "extra_args": (),
        "sandbox": "read-only",
        "base_url": "",
        "wire_api": "responses",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_no_endpoint_means_no_provider_flags() -> None:
    engine = CodexEngine(
        SimpleNamespace(engine=_engine()), SimpleNamespace(replaces_environment=True)
    )

    argv = engine._argv(worktree=".", effort="low", sandbox="read-only")

    assert "model_provider=touchstone" not in argv


def test_a_configured_endpoint_becomes_a_named_codex_provider() -> None:
    engine = CodexEngine(
        SimpleNamespace(engine=_engine(base_url="https://api.example.test/v1")),
        SimpleNamespace(replaces_environment=True),
    )

    argv = engine._argv(worktree=".", effort="low", sandbox="read-only")

    assert "model_providers.touchstone.base_url=https://api.example.test/v1" in argv
    assert "model_providers.touchstone.wire_api=responses" in argv
    assert "model_provider=touchstone" in argv
    # The key is named, never inlined: it travels in the environment.
    assert "model_providers.touchstone.env_key=OPENAI_API_KEY" in argv
    assert not any("sk-" in item for item in argv)


def test_claude_reads_its_endpoint_from_the_environment() -> None:
    source = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k"}

    configured = engine_environment("claude", source, base_url="https://api.minimax.io/anthropic")
    plain = engine_environment("claude", source)

    assert configured["ANTHROPIC_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert "ANTHROPIC_BASE_URL" not in plain


def test_an_endpoint_is_never_handed_to_the_wrong_engine() -> None:
    source = {"PATH": "/bin", "HOME": "/h", "OPENAI_API_KEY": "k"}

    # Codex is told through its provider flags, so nothing leaks into its env.
    assert "ANTHROPIC_BASE_URL" not in engine_environment(
        "codex", source, base_url="https://api.example.test/v1"
    )


@pytest.mark.parametrize(
    ("engine", "reason"),
    [
        ({"base_url": "http://elsewhere.test/v1"}, "https"),
        ({"base_url": "ftp://x/v1"}, "absolute http or https"),
        ({"base_url": "https://x.test/v1?token=leak"}, "no query or userinfo"),
        ({"base_url": "https://u:p@x.test/v1"}, "no query or userinfo"),
        ({"wire_api": "grpc"}, "'chat' or 'responses'"),
        ({"base_url": "https://x.test/v1", "wire_api": "chat"}, "not supported by Codex"),
    ],
)
def test_an_unusable_endpoint_is_refused(engine: dict[str, str], reason: str) -> None:
    with pytest.raises(ConfigError, match=reason):
        _model_endpoint({"name": "codex", **engine})


def test_a_loopback_endpoint_may_use_plain_http() -> None:
    _model_endpoint({"name": "codex", "base_url": "http://127.0.0.1:8931/v1"})
    _model_endpoint({"name": "codex", "base_url": "http://localhost:8931/v1"})
