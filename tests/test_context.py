from __future__ import annotations

from pathlib import Path

from tests.test_config import _valid_config, _write
from touchstone.config import load_config
from touchstone.harnesses import HarnessContext
from touchstone.nodes.context import bind_harness, configure, current, reset


def test_explicit_config_is_the_context_seen_by_graph_nodes(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / "explicit.toml", _valid_config()))

    bound = configure(config)
    try:
        assert current() is bound
        assert current().config.source.path == config.source.path
    finally:
        reset()


def test_one_resolved_harness_is_bound_for_every_graph_node(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / "explicit.toml", _valid_config()))
    entrypoint = tmp_path / "AGENTS.md"
    entrypoint.write_text("rules\n", encoding="utf-8")
    configure(config)
    context = HarnessContext(
        mode="embedded",
        source="repository",
        entrypoint=entrypoint,
        revision="abc123",
        context_root=tmp_path,
        evidence=("generated-source:abc123",),
    )
    try:
        bound = bind_harness(context)

        assert current() is bound
        assert current().harness is context
        assert "mode: embedded" in current().harness_prompt()
        assert "revision: abc123" in current().harness_prompt()
        assert str(tmp_path) not in current().harness_prompt()
    finally:
        reset()
