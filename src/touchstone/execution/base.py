"""The contract every executor honours."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Result:
    """What a command left behind.

    `timed_out` is separate from a non-zero code on purpose. A session killed
    at its ceiling and a session that failed on its own are different events —
    one says the work was too big, the other says the work was wrong — and
    collapsing them into "it failed" is how a wedged engine gets diagnosed as
    a bad prompt.
    """

    code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, limit: int = 400) -> str:
        """The end of whatever it said, for a log line that has to be short."""
        text = (self.stderr or self.stdout).strip()
        return text[-limit:]


@runtime_checkable
class Executor(Protocol):
    """Runs a command somewhere, and can read and write files there.

    File access is part of the interface rather than something callers reach
    around, because the two implementations disagree about what a path means:
    locally it is a path, and over ssh it is a path on another machine. A
    caller that opened files directly would work in testing and silently read
    the wrong machine's disk in production.
    """

    #: For logs. `local`, or `ssh my-server`.
    where: str

    #: Whether `env` replaces the command's environment rather than adding to it.
    #:
    #: A local subprocess is handed exactly the mapping it is given. An ssh
    #: command cannot be: its environment belongs to the far side, and the best
    #: this end can do is prepend assignments — which add to the remote
    #: environment instead of replacing it, override whatever
    #: `execution.ssh.env` configured, and put every value on a remote command
    #: line. A caller that passes `env` to scrub or isolate must check this
    #: first; passing a locally derived environment to a remote command
    #: achieves neither.
    replaces_environment: bool

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        stdin_null: bool = True,
        env: dict[str, str] | None = None,
    ) -> Result:
        """Run one command and wait for it.

        `stdin_null` defaults to true and closing stdin is not tidiness. Codex
        appends piped stdin to its prompt, so with stdin left open it blocks on
        a read that never returns — invisibly, because from a terminal stdin is
        a tty and reaches EOF. It cost fifty minutes and one silent hang before
        anyone noticed.
        """

    def read_text(self, path: str) -> str | None:
        """A file's contents, or None when it is not there."""

    def write_text(self, path: str, text: str) -> None:
        """Replace a file's contents."""

    def exists(self, path: str) -> bool: ...
