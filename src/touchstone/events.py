"""Small, append-only run records with an intentionally narrow data surface."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    kind: str
    config_fingerprint: str
    loop: str = ""
    engine: str = ""
    model: str = ""
    effort: str = ""
    executor: str = ""
    host: str = ""
    outcome: str = ""
    duration_seconds: float | None = None
    timed_out: bool | None = None
    cost: float | None = None
    risk_from: str = ""
    risk_to: str = ""
    verdict: str = ""
    pr: int | None = None
    detail: str = ""
    ts: str = ""


class EventLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    def append(self, event: RunEvent) -> None:
        row = {key: value for key, value in asdict(event).items() if value not in {"", None}}
        row.setdefault("ts", dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


def run_event(
    config: Any,
    *,
    run_id: str,
    kind: str,
    loop: str = "",
    outcome: str = "",
    duration_seconds: float | None = None,
    timed_out: bool | None = None,
    cost: float | None = None,
    risk_from: str = "",
    risk_to: str = "",
    verdict: str = "",
    pr: int | None = None,
    detail: str = "",
) -> RunEvent:
    execution = config.execution
    host = platform.node()
    if execution.target == "ssh" and execution.ssh is not None:
        host = execution.ssh.host
    return RunEvent(
        run_id=run_id,
        kind=kind,
        config_fingerprint=config_fingerprint(config),
        loop=loop,
        engine=config.engine.name,
        model=config.engine.model,
        effort=config.engine.audit_effort,
        executor=execution.target,
        host=host,
        outcome=outcome,
        duration_seconds=duration_seconds,
        timed_out=timed_out,
        cost=cost,
        risk_from=risk_from,
        risk_to=risk_to,
        verdict=verdict,
        pr=pr,
        detail=detail,
    )


def config_fingerprint(config: Any) -> str:
    safe = {
        "version": config.source.schema_version,
        "forge": {
            "slug": config.forge.slug,
            "default_branch": config.forge.default_branch,
        },
        "engine": {
            "name": config.engine.name,
            "model": config.engine.model,
            "effort": config.engine.audit_effort,
        },
        "execution": config.execution.target,
        "loops": {name: {"schedule": loop.schedule} for name, loop in sorted(config.loops.items())},
    }
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


__all__ = ["EventLog", "RunEvent", "config_fingerprint", "run_event"]
