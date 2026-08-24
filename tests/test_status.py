from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from touchstone.config import LoopConfig
from touchstone.events import EventLog, run_event
from touchstone.forge import OperationResult, PullState
from touchstone.ledger import Ledger, LifecycleEvent, finding_id
from touchstone.scheduling.base import SchedulerStatus
from touchstone.status import collect_status


class MemoryForge:
    def __init__(self, pull: PullState) -> None:
        self.pull_state = pull

    def pull(self, number: int) -> PullState | None:
        return self.pull_state if self.pull_state.number == number else None

    def close(self, number: int, comment: str) -> OperationResult:
        return OperationResult(True)


class MemoryScheduler:
    def status(self, config) -> SchedulerStatus:  # type: ignore[no-untyped-def]
        return SchedulerStatus(
            adapter="launchd",
            supported=True,
            installed=(Path(config.state_dir) / "touchstone-code.plist",),
        )


def test_status_reconciles_live_pull_truth_before_projecting(tmp_path: Path) -> None:
    loop = LoopConfig(
        name="code",
        brief="builtin:code-audit",
        label="touchstone:audit",
        config_dir=tmp_path,
    )
    config = SimpleNamespace(
        state_dir=tmp_path,
        source=SimpleNamespace(schema_version=1),
        forge=SimpleNamespace(slug="acme/widgets", default_branch="main", reap_after_hours=6),
        engine=SimpleNamespace(name="codex", model="gpt-test", audit_effort="high"),
        execution=SimpleNamespace(target="local", ssh=None),
        loops={"code": loop},
    )
    ledger = Ledger(tmp_path / "ledger.jsonl")
    identifier = finding_id("code", "Broken invariant")
    ledger.append(
        LifecycleEvent(
            finding_id=identifier,
            state="armed",
            title="Broken invariant",
            loop="code",
            risk="low",
            pr=12,
            head_sha="abc123",
        )
    )
    forge = MemoryForge(
        PullState(
            number=12,
            head_sha="abc123",
            branch="touchstone/run-1",
            draft=False,
            check_state="success",
            merged_at="2026-08-24T10:00:00Z",
            closed=False,
            created_at="2026-08-24T09:00:00Z",
            url="https://github.com/acme/widgets/pull/12",
        )
    )
    EventLog(tmp_path / "events.jsonl").append(
        run_event(config, run_id="run-1", kind="finished", loop="code", outcome="armed")
    )

    report = collect_status(
        config,
        SimpleNamespace(forge=forge, ledger=ledger),
        now=dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC),
        scheduler=MemoryScheduler(),
    )

    payload = report.to_dict()
    assert payload["findings"][0]["state"] == "merged"
    assert payload["last_runs"][0]["outcome"] == "armed"
    assert payload["scheduler"]["adapter"] == "launchd"
    assert payload["scheduler"]["installed"] == [str(tmp_path / "touchstone-code.plist")]
