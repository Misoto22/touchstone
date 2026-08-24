"""What every node needs, assembled once.

Nodes receive graph state, which is checkpointed and therefore has to stay
JSON-shaped. Executors, engines and open sockets are none of those things, so
they live here and are looked up by name instead of travelling in the state.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from touchstone import engines, execution
from touchstone.config import Config, LoopConfig, load
from touchstone.forge import Forge
from touchstone.ledger import Ledger


@dataclass(frozen=True, slots=True)
class Context:
    config: Config
    executor: execution.Executor
    engine: engines.Engine
    forge: Forge
    ledger: Ledger

    def loop(self, name: str) -> LoopConfig:
        return self.config.loop(name)


_ACTIVE: ContextVar[Context | None] = ContextVar("touchstone_context", default=None)


def _build(config: Config) -> Context:
    executor = execution.build(config)
    return Context(
        config=config,
        executor=executor,
        engine=engines.build(config, executor),
        forge=Forge(config.forge.slug, executor),
        ledger=Ledger(Path(config.state_dir) / "ledger.jsonl"),
    )


def configure(config: Config) -> Context:
    """Bind the explicitly loaded CLI config for every graph node in this context."""
    context = _build(config)
    _ACTIVE.set(context)
    return context


def reset() -> None:
    """Drop an explicit binding, primarily for isolated callers and tests."""
    _ACTIVE.set(None)


@lru_cache(maxsize=1)
def _discovered() -> Context:
    return _build(load())


def current() -> Context:
    """The explicitly bound context, or one discovered for direct graph use.

    Cached because building it is cheap but reading the configuration twice
    invites the two halves of a run disagreeing about which model they are
    using — which is exactly the kind of thing nobody notices until a log says
    one name and a bill says another.
    """
    return _ACTIVE.get() or _discovered()


__all__ = ["Context", "configure", "current", "reset"]
