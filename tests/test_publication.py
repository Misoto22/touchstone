from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from touchstone.execution.local import LocalExecutor
from touchstone.forge import ForgeUnavailable, OperationResult, PullState
from touchstone.ledger import Ledger, finding_id
from touchstone.lifecycle import PublicationRequest, RepositoryLifecycle
from touchstone.nodes import publish as publish_node


class MemoryForge:
    def __init__(self, *, arm_ok: bool = True, existing: PullState | None = None) -> None:
        self.arm_ok = arm_ok
        self.existing = existing
        self.created_pull_count = 0
        self.transitions: list[str] = []

    def pull(self, number: int) -> PullState | None:
        return self.existing if self.existing and self.existing.number == number else None

    def pull_for_branch(self, branch: str) -> PullState | None:
        return self.existing if self.existing and self.existing.branch == branch else None

    def create_pull(self, **kwargs) -> int:  # type: ignore[no-untyped-def]
        self.created_pull_count += 1
        self.transitions.append(f"create:{'draft' if kwargs['draft'] else 'ready'}")
        return 12

    def arm_auto_merge(self, number: int) -> OperationResult:
        self.transitions.append(f"auto-merge:{number}")
        return OperationResult(self.arm_ok, "auto-merge disabled" if not self.arm_ok else "")

    def to_draft(self, number: int) -> OperationResult:
        self.transitions.append(f"draft:{number}")
        return OperationResult(True)

    def add_label(self, number: int, label: str) -> OperationResult:
        self.transitions.append(f"label:{number}:{label}")
        return OperationResult(True)

    def close(self, number: int, comment: str) -> OperationResult:
        return OperationResult(True)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Test Author")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "switch", "-c", "touchstone/run-1")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    return repo


def _request(repo: Path, *, risk: str = "low", verdict: str = "approve") -> PublicationRequest:
    title = "Broken invariant"
    return PublicationRequest(
        finding_id=finding_id("code", title),
        loop="code",
        branch="touchstone/run-1",
        worktree=repo,
        base="main",
        label="touchstone:audit",
        escalation_label="touchstone:needs-review",
        risk=risk,
        verdict=verdict,
        title=title,
        commit_subject="fix: repair the invariant",
        summary="Repairs the invariant.",
        rationale="The old value drifted from its source.",
        review_reason="The change is isolated and covered.",
    )


def test_failed_auto_merge_is_held_and_not_armed(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    ledger = Ledger(tmp_path / "events.jsonl")
    forge = MemoryForge(arm_ok=False)
    lifecycle = RepositoryLifecycle(forge, ledger, reap_after_hours=6, executor=LocalExecutor())

    result = lifecycle.publish(_request(repo))

    assert result.outcome == "held"
    assert "auto-merge disabled" in result.detail
    assert ledger.projection(result.finding_id).state == "proposed"  # type: ignore[union-attr]


def test_retry_reuses_existing_pull_for_branch(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    existing = PullState(
        number=12,
        head_sha="old",
        branch="touchstone/run-1",
        draft=False,
        check_state="pending",
        merged_at=None,
        closed=False,
        created_at="2026-08-24T00:00:00Z",
        url="https://github.com/acme/widgets/pull/12",
    )
    forge = MemoryForge(existing=existing)
    lifecycle = RepositoryLifecycle(
        forge, Ledger(tmp_path / "events.jsonl"), reap_after_hours=6, executor=LocalExecutor()
    )

    result = lifecycle.publish(_request(repo))

    assert result.outcome == "armed"
    assert result.pr == 12
    assert forge.created_pull_count == 0


def test_high_risk_publication_opens_a_draft_and_parks(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    forge = MemoryForge()
    lifecycle = RepositoryLifecycle(
        forge, Ledger(tmp_path / "events.jsonl"), reap_after_hours=6, executor=LocalExecutor()
    )

    result = lifecycle.publish(_request(repo, risk="high", verdict="skipped"))

    assert result.outcome == "parked"
    assert forge.transitions[0] == "create:draft"
    assert "auto-merge:12" not in forge.transitions


def test_publication_holds_when_existing_pull_state_is_unavailable(tmp_path: Path) -> None:
    class UnavailableForge(MemoryForge):
        def pull_for_branch(self, branch: str) -> PullState | None:
            raise ForgeUnavailable("pull lookup unavailable")

    repo = _worktree(tmp_path)
    forge = UnavailableForge()
    lifecycle = RepositoryLifecycle(
        forge, Ledger(tmp_path / "events.jsonl"), reap_after_hours=6, executor=LocalExecutor()
    )

    result = lifecycle.publish(_request(repo))

    assert result.outcome == "held"
    assert "verify existing pull" in result.detail
    assert forge.created_pull_count == 0


def test_node_builds_publication_from_project_configuration() -> None:
    context = SimpleNamespace(
        config=SimpleNamespace(
            forge=SimpleNamespace(default_branch="trunk", escalation_label="ops:review"),
            git=SimpleNamespace(author_name="Touchstone Bot", author_email="bot@example.com"),
        ),
        loop=lambda name: SimpleNamespace(name=name, label="automation:audit"),
    )
    state = {
        "loop": "architecture",
        "branch": "touchstone/run-2",
        "worktree": "/tmp/worktree",
        "risk": "low",
        "verdict": "approve",
        "verdict_reason": "covered by a focused regression test",
        "finding": {
            "title": "Configuration drift",
            "commit_subject": "fix: prevent configuration drift",
            "summary": "Keeps configuration aligned.",
            "rationale": "One source of truth is safer.",
        },
    }

    request = publish_node._request(state, context)  # type: ignore[arg-type]

    assert request.base == "trunk"
    assert request.label == "automation:audit"
    assert request.escalation_label == "ops:review"
    assert (request.author_name, request.author_email) == (
        "Touchstone Bot",
        "bot@example.com",
    )
