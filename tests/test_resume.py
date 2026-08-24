from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from touchstone.forge import OperationResult, PullState
from touchstone.ledger import Ledger, LifecycleEvent, finding_id
from touchstone.lifecycle import RepositoryLifecycle, ResumeRequest


class MemoryForge:
    def __init__(self, pull: PullState, *, arm_ok: bool = True) -> None:
        self.live = pull
        self.arm_ok = arm_ok
        self.transitions: list[str] = []

    def pull(self, number: int) -> PullState | None:
        return self.live if self.live.number == number else None

    def mark_ready(self, number: int) -> OperationResult:
        self.transitions.append(f"ready:{number}")
        return OperationResult(True)

    def arm_auto_merge(self, number: int) -> OperationResult:
        self.transitions.append(f"auto-merge:{number}")
        return OperationResult(self.arm_ok, "auto-merge disabled" if not self.arm_ok else "")

    def close(self, number: int, comment: str) -> OperationResult:
        self.transitions.append(f"close:{number}")
        return OperationResult(True)


def _pull(*, head: str = "abc123", draft: bool = True) -> PullState:
    return PullState(
        number=12,
        head_sha=head,
        branch="touchstone/run-1",
        draft=draft,
        check_state="success",
        merged_at=None,
        closed=False,
        created_at="2026-08-24T00:00:00Z",
        url="https://github.com/acme/widgets/pull/12",
    )


def _lifecycle(tmp_path: Path, forge: MemoryForge) -> tuple[RepositoryLifecycle, Ledger, str]:
    ledger = Ledger(tmp_path / "events.jsonl")
    identifier = finding_id("code", "Broken invariant")
    ledger.append(
        LifecycleEvent(
            finding_id=identifier,
            state="parked",
            title="Broken invariant",
            loop="code",
            risk="medium",
            pr=12,
            head_sha="abc123",
        )
    )
    return RepositoryLifecycle(forge, ledger, reap_after_hours=6), ledger, identifier


def test_resume_refuses_a_pull_whose_head_changed(tmp_path: Path) -> None:
    forge = MemoryForge(_pull(head="changed"))
    lifecycle, ledger, identifier = _lifecycle(tmp_path, forge)

    result = lifecycle.resume(ResumeRequest(identifier, 12, "approve", "abc123", identifier))

    assert result.outcome == "held"
    assert "changed" in result.detail
    assert forge.transitions == []
    assert ledger.projection(identifier).state == "awaiting_human"  # type: ignore[union-attr]


def test_resume_marks_the_same_reviewed_draft_ready_without_auto_merge(tmp_path: Path) -> None:
    forge = MemoryForge(_pull())
    lifecycle, ledger, identifier = _lifecycle(tmp_path, forge)

    result = lifecycle.resume(ResumeRequest(identifier, 12, "approve", "abc123", identifier))

    assert result.outcome == "awaiting_checks"
    assert forge.transitions == ["ready:12"]
    assert ledger.projection(identifier).state == "awaiting_checks"  # type: ignore[union-attr]


def test_resume_never_calls_auto_merge_even_when_repository_disables_it(tmp_path: Path) -> None:
    forge = MemoryForge(_pull(), arm_ok=False)
    lifecycle, ledger, identifier = _lifecycle(tmp_path, forge)

    result = lifecycle.resume(ResumeRequest(identifier, 12, "approve", "abc123", identifier))

    assert result.outcome == "awaiting_checks"
    assert forge.transitions == ["ready:12"]
    assert ledger.projection(identifier).state == "awaiting_checks"  # type: ignore[union-attr]


def test_resume_close_records_the_completed_operator_decision(tmp_path: Path) -> None:
    forge = MemoryForge(_pull())
    lifecycle, ledger, identifier = _lifecycle(tmp_path, forge)

    result = lifecycle.resume(ResumeRequest(identifier, 12, "close", "abc123", identifier))

    assert result.outcome == "closed"
    assert forge.transitions == ["close:12"]
    assert ledger.projection(identifier).state == "closed"  # type: ignore[union-attr]


def test_resume_refuses_a_mismatched_candidate_lineage(tmp_path: Path) -> None:
    forge = MemoryForge(_pull())
    lifecycle, ledger, identifier = _lifecycle(tmp_path, forge)

    result = lifecycle.resume(
        ResumeRequest(identifier, 12, "approve", "abc123", "different-candidate")
    )

    assert result.outcome == "held"
    assert "lineage" in result.detail
    assert forge.transitions == []
    assert ledger.projection(identifier).state == "awaiting_human"  # type: ignore[union-attr]


def test_health_gate_checks_only_configured_workflows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from touchstone import runner

    calls: list[tuple[str, str | None]] = []

    class HealthForge:
        def latest_run(self, workflow: str, *, branch: str | None = None) -> str:
            calls.append((workflow, branch))
            return "success"

    config = SimpleNamespace(
        forge=SimpleNamespace(
            default_branch="trunk", required_workflows=("quality.yml", "deploy.yml")
        )
    )
    monkeypatch.setattr(runner, "current", lambda: SimpleNamespace(forge=HealthForge()))

    runner._health_gate(config)  # type: ignore[arg-type]

    assert calls == [("quality.yml", "trunk"), ("deploy.yml", "trunk")]


def test_health_gate_refuses_unattended_publish_without_a_workflow(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pytest

    from touchstone import runner

    config = SimpleNamespace(forge=SimpleNamespace(default_branch="trunk", required_workflows=()))
    monkeypatch.setattr(runner, "current", lambda: SimpleNamespace(forge=object()))

    with pytest.raises(runner.Held, match="no required workflows"):
        runner._health_gate(config)  # type: ignore[arg-type]


def test_publication_gate_requires_setup_labels_but_not_auto_merge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pytest

    from touchstone import runner

    class ForgeWithoutAutoMerge:
        def repository_info(self):  # type: ignore[no-untyped-def]
            return {"autoMergeAllowed": False}

        def labels(self) -> set[str]:
            return set()

    config = SimpleNamespace(forge=SimpleNamespace(escalation_label="touchstone:needs-review"))
    loop = SimpleNamespace(label="touchstone:audit")
    monkeypatch.setattr(runner, "current", lambda: SimpleNamespace(forge=ForgeWithoutAutoMerge()))

    with pytest.raises(runner.Held, match="labels"):
        runner._publication_gate(config, loop)  # type: ignore[arg-type]


def test_runner_resume_uses_the_shared_lock_and_health_gate() -> None:
    import inspect

    from touchstone import runner

    source = inspect.getsource(runner.resume)
    assert "_lock(state_dir)" in source
    assert "_health_gate(config)" in source
    assert "_publication_gate(config" in source
    assert "shutil.rmtree(lock" in source
    assert "execute(config, loop=reanalysis_loop)" in source
