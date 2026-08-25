"""What every node needs, assembled once.

Nodes receive graph state, which is checkpointed and therefore has to stay
JSON-shaped. Executors, engines and open sockets are none of those things, so
they live here and are looked up by name instead of travelling in the state.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
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
    #: The unnamed engine. A Loop that names a pool member gets its own through
    #: `engine_for`; this stays the answer when no Loop is in hand.
    engine: engines.Engine
    forge: Forge
    ledger: Ledger
    _engines: dict[object, engines.Engine] = field(default_factory=dict)

    def loop(self, name: str) -> LoopConfig:
        return self.config.loop(name)

    def engine_for(self, loop: str | None = None) -> engines.Engine:
        """The engine instance that Loop's sessions run on.

        Built on demand and remembered, because a Loop naming another provider
        needs another CLI wrapper entirely, and building one per session would
        re-read the same configuration for every node in the graph.
        """

        resolved = self.config.engine_for(loop)
        cached = self._engines.get(resolved)
        if cached is None:
            cached = engines.build(self.config, self.executor, resolved)
            self._engines[resolved] = cached
        return cached

    @staticmethod
    def build(config: Config) -> Context:
        """Assemble the context for a configuration without binding it."""

        return _build(config)


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
