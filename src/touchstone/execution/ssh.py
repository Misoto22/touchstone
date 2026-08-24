"""Running on another machine.

The same interface as local, and deliberately not a thin wrapper that pastes a
command into a shell string. Every argument is quoted individually, because a
brief is one of those arguments: several kilobytes of prose containing
backticks, quotes, dollar signs and newlines, going to a remote shell. String
interpolation there is not a style question, it is remote code execution.
"""

from __future__ import annotations

import shlex

from touchstone.config import SshConfig
from touchstone.execution.base import Result
from touchstone.execution.local import LocalExecutor


class SshExecutor:
    #: A remote environment is configured by `execution.ssh.env`, not replaced
    #: from here.
    replaces_environment = False

    def __init__(self, config: SshConfig) -> None:
        self._config = config
        self._local = LocalExecutor()
        self.where = f"ssh {config.host}"

    def _ssh(self, remote: str, *, timeout: int | None) -> Result:
        argv = [
            "ssh",
            "-o",
            f"ConnectTimeout={self._config.connect_timeout}",
            "-o",
            "BatchMode=yes",
        ]
        if self._config.identity_file:
            argv += ["-i", self._config.identity_file]
        argv += [self._config.host, remote]
        # A local timeout on a remote command leaves the remote side running.
        # The engine's own ceiling is applied over there as well, in `run`;
        # this one only bounds how long we wait for the connection to answer.
        return self._local.run(argv, timeout=timeout)

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        stdin_null: bool = True,
        env: dict[str, str] | None = None,
    ) -> Result:
        parts: list[str] = []
        for key, value in self._config.env:
            parts.append(f"{key}={shlex.quote(value)}")
        for key, value in (env or {}).items():
            parts.append(f"{key}={shlex.quote(value)}")

        command = " ".join([*parts, shlex.join(argv)])
        if cwd:
            command = f"cd {shlex.quote(cwd)} && {command}"
        if timeout:
            # Bounded on the far side too. Without this a hung session survives
            # our giving up on it, holds whatever it holds, and the next run
            # finds a lock whose pid is alive on a machine we are not watching.
            command = f"timeout {int(timeout)} sh -c {shlex.quote(command)}"
        if stdin_null:
            command = f"{command} </dev/null"

        result = self._ssh(command, timeout=(timeout + 60) if timeout else None)
        if result.code == 124:
            return Result(result.code, result.stdout, result.stderr, timed_out=True)
        return result

    def read_text(self, path: str) -> str | None:
        result = self._ssh(f"cat {shlex.quote(path)}", timeout=60)
        return result.stdout if result.ok else None

    def write_text(self, path: str, text: str) -> None:
        quoted = shlex.quote(path)
        # A heredoc with a quoted delimiter: the content is never expanded, and
        # the delimiter is one no prose will contain.
        remote = (
            f"mkdir -p $(dirname {quoted}) && cat > {quoted} <<'TOUCHSTONE_LOOP_EOF'\n"
            f"{text}\nTOUCHSTONE_LOOP_EOF"
        )
        result = self._ssh(remote, timeout=120)
        if not result.ok:
            raise OSError(f"could not write {path} on {self._config.host}: {result.tail()}")

    def exists(self, path: str) -> bool:
        return self._ssh(f"test -e {shlex.quote(path)}", timeout=60).ok
