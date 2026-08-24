"""Where a command actually runs.

One interface, two implementations, and the point of the interface is that
nothing above it can tell which one it has. The bash version this replaces
assumed the machine holding the git clone was also the machine running the
model, and that assumption is wrong in the direction that matters: telling a
latent defect from a live one needs the production database, and only the
server can reach it.
"""

from touchstone.execution.base import Executor, Result
from touchstone.execution.local import LocalExecutor
from touchstone.execution.ssh import SshExecutor

__all__ = ["Executor", "LocalExecutor", "Result", "SshExecutor", "build"]


def build(config):  # type: ignore[no-untyped-def]
    """The executor a configuration asks for."""
    from touchstone.config import ConfigError

    if config.execution.target == "local":
        return LocalExecutor()
    if config.execution.ssh is None:  # pragma: no cover - ExecutionConfig rejects this
        raise ConfigError("execution.target is 'ssh' but no [execution.ssh] section was given")
    return SshExecutor(config.execution.ssh)
