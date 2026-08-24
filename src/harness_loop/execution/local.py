"""Running on the machine this process is on."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness_loop.execution.base import Result


class LocalExecutor:
    where = "local"

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        stdin_null: bool = True,
        env: dict[str, str] | None = None,
    ) -> Result:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL if stdin_null else None,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            return Result(
                code=124,
                stdout=_text(expired.stdout),
                stderr=_text(expired.stderr),
                timed_out=True,
            )
        return Result(code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)

    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write_text(self, path: str, text: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def exists(self, path: str) -> bool:
        return Path(path).exists()


def _text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
