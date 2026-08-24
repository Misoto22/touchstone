from __future__ import annotations

from pathlib import Path

from tests.test_config import _valid_config, _write
from touchstone.config import load_config
from touchstone.nodes.context import configure, current, reset


def test_explicit_config_is_the_context_seen_by_graph_nodes(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / "explicit.toml", _valid_config()))

    bound = configure(config)
    try:
        assert current() is bound
        assert current().config.source.path == config.source.path
    finally:
        reset()
