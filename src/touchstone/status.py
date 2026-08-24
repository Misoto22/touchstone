"""Read-only operator status plus an explicit reconciliation operation."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from touchstone.events import EventLog
from touchstone.lifecycle import RepositoryLifecycle


@dataclass(frozen=True, slots=True)
class StatusReport:
    findings: tuple[dict[str, Any], ...]
    last_runs: tuple[dict[str, Any], ...]
    reconciliation: dict[str, dict[str, list[int]]]
    scheduler: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": list(self.findings),
            "last_runs": list(self.last_runs),
            "reconciliation": self.reconciliation,
            "scheduler": self.scheduler,
        }


def collect_status(
    config: Any,
    context: Any,
    *,
    now: dt.datetime | None = None,
    scheduler: Any | None = None,
) -> StatusReport:
    findings = tuple(
        asdict(projection)
        for projection in sorted(
            context.ledger.projections().values(), key=lambda item: (item.loop, item.ts, item.title)
        )
    )
    finished = [
        row
        for row in EventLog(Path(config.state_dir) / "events.jsonl").rows()
        if row.get("kind") == "finished"
    ]
    last_by_loop: dict[str, dict[str, Any]] = {}
    for row in finished:
        last_by_loop[str(row.get("loop") or "unknown")] = row
    scheduler_payload = None
    if scheduler is not None:
        native = scheduler.status(config)
        scheduler_payload = asdict(native)
        scheduler_payload["installed"] = [str(path) for path in native.installed]
        scheduler_payload["missing"] = [str(path) for path in native.missing]
    return StatusReport(
        findings=findings,
        last_runs=tuple(last_by_loop[name] for name in sorted(last_by_loop)),
        reconciliation={},
        scheduler=scheduler_payload,
    )


def reconcile_status(
    config: Any,
    context: Any,
    *,
    now: dt.datetime | None = None,
) -> dict[str, dict[str, list[int]]]:
    observed_at = now or dt.datetime.now(dt.UTC)
    result: dict[str, dict[str, list[int]]] = {}
    lifecycle = RepositoryLifecycle(
        context.forge,
        context.ledger,
        reap_after_hours=config.forge.reap_after_hours,
    )
    for name, loop in sorted(config.loops.items()):
        report = lifecycle.reconcile(loop, observed_at)
        result[name] = {key: list(value) for key, value in asdict(report).items()}
    return result


__all__ = ["StatusReport", "collect_status", "reconcile_status"]
