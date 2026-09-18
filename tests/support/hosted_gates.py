from __future__ import annotations

from pathlib import Path
from typing import Any

from touchstone.config import EngineConfig
from touchstone.hosted.gates import GATES_FILE, ForgeGates


def with_engine(config: Any, engine: EngineConfig | None = None) -> Any:
    """Give a stand-in configuration the one engine every Loop runs on."""

    config.engine = engine or EngineConfig(name="codex")
    config.engine_for = lambda _loop=None: config.engine
    return config


def open_gates(config: Any, **overrides: Any) -> ForgeGates:
    """Write the forge gates a Preparation Stage records when nothing is held."""

    gates = ForgeGates(
        overrides.get("health", ""),
        overrides.get("slots", {name: () for name in config.loops}),
    )
    gates.write(Path(config.repo_path) / ".touchstone" / "hosted" / "prepare" / GATES_FILE)
    return gates
