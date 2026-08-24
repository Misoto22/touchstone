"""What every node needs, assembled once.

Nodes receive graph state, which is checkpointed and therefore has to stay
JSON-shaped. Executors, engines and open sockets are none of those things, so
they live here and are looked up by name instead of travelling in the state.
"""

from __future__ import annotations

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


@lru_cache(maxsize=1)
def current() -> Context:
    """The context this process runs under.

    Cached because building it is cheap but reading the configuration twice
    invites the two halves of a run disagreeing about which model they are
    using — which is exactly the kind of thing nobody notices until a log says
    one name and a bill says another.
    """
    config = load()
    executor = execution.build(config)
    return Context(
        config=config,
        executor=executor,
        engine=engines.build(config, executor),
        forge=Forge(config.forge.slug, executor),
        ledger=Ledger(Path(config.state_dir) / "ledger.jsonl"),
    )
