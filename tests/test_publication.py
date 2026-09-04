from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from touchstone.config import GitConfig
from touchstone.execution.base import Result
from touchstone.execution.local import LocalExecutor
from touchstone.forge import ForgeUnavailable, OperationResult, PullState
from touchstone.ledger import Ledger, finding_id
from touchstone.lifecycle import PublicationRequest, PublicationResult, RepositoryLifecycle
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


def test_approved_publication_does_not_enable_auto_merge(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    ledger = Ledger(tmp_path / "events.jsonl")
    forge = MemoryForge(arm_ok=False)
    lifecycle = RepositoryLifecycle(forge, ledger, reap_after_hours=6, executor=LocalExecutor())

    result = lifecycle.publish(_request(repo))

    assert result.outcome == "awaiting_checks"
    assert "auto-merge:12" not in forge.transitions
    assert ledger.projection(result.finding_id).state == "awaiting_checks"  # type: ignore[union-attr]


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

    assert result.outcome == "awaiting_checks"
    assert result.pr == 12
    assert forge.created_pull_count == 0


def test_high_risk_publication_opens_a_draft_and_parks(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    forge = MemoryForge()
    lifecycle = RepositoryLifecycle(
        forge, Ledger(tmp_path / "events.jsonl"), reap_after_hours=6, executor=LocalExecutor()
    )

    result = lifecycle.publish(_request(repo, risk="high", verdict="skipped"))

    assert result.outcome == "awaiting_human"
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

    assert result.outcome == "failed"
    assert "verify existing pull" in result.detail
    assert result.partial is True
    assert result.branch == "touchstone/run-1"
    assert forge.created_pull_count == 0


def test_node_preserves_branch_only_partial_publication_identity() -> None:
    payload = publish_node._publication_payload(
        PublicationResult(
            "failed",
            "candidate-1",
            None,
            "a" * 40,
            "pull creation unavailable",
            partial=True,
            branch="touchstone/candidate-1",
        )
    )

    assert payload["partial"] is True
    assert payload["branch"] == "touchstone/candidate-1"
    assert payload["reviewed_head_sha"] == "a" * 40


def test_dry_run_rehearsal_runs_preparation_and_validation_before_diff(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    config = SimpleNamespace(
        state_dir=tmp_path,
        forge=SimpleNamespace(default_branch="main"),
        loop=lambda _name: SimpleNamespace(targets=("app",)),
    )
    context = SimpleNamespace(config=config, executor=object())
    monkeypatch.setattr(publish_node, "current", lambda: context)
    monkeypatch.setattr(
        "touchstone.validation.prepare",
        lambda *_args, **_kwargs: (
            calls.append("prepare") or SimpleNamespace(outcome="completed", results=())
        ),
    )
    monkeypatch.setattr(
        "touchstone.validation.validate_affected",
        lambda *_args, **_kwargs: (
            calls.append("validate") or SimpleNamespace(blocked=True, results=())
        ),
    )

    result = publish_node.rehearse(
        {"loop": "code", "worktree": str(tmp_path), "risk": "low"}, would="publish"
    )

    assert calls == ["prepare", "validate"]
    assert result["outcome"] == "blocked"
    assert not (tmp_path / "dry-run.diff").exists()


def test_verified_publication_runs_no_repository_hooks_or_staging_with_write_token(
    tmp_path: Path,
) -> None:
    repo = _worktree(tmp_path)
    _git(repo, "add", "-A")

    class RecordingExecutor(LocalExecutor):
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.commands.append(argv)
            return super().run(argv, **kwargs)

    executor = RecordingExecutor()
    lifecycle = RepositoryLifecycle(
        MemoryForge(), Ledger(tmp_path / "events.jsonl"), reap_after_hours=6, executor=executor
    )

    result = lifecycle.publish(replace(_request(repo), pre_staged=True))

    assert result.outcome == "awaiting_checks"
    assert not any(command[-2:] == ["add", "-A"] for command in executor.commands)
    commit = next(command for command in executor.commands if "commit" in command)
    push = next(command for command in executor.commands if "push" in command)
    assert "--no-verify" in commit
    assert "--no-verify" in push


def test_isolated_publication_ignores_persisted_remote_and_scrubs_state_key(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    class RecordingExecutor:
        where = "local"

        def __init__(self) -> None:
            self.calls: list[tuple[list[str], dict[str, str] | None]] = []

        def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append((argv, kwargs.get("env")))
            code = 1 if "--quiet" in argv and "diff" in argv else 0
            return Result(code, "", "")

    monkeypatch.setenv("GH_TOKEN", "publish-token")
    monkeypatch.setenv("TOUCHSTONE_STATE_KEY", "state-secret")
    executor = RecordingExecutor()
    lifecycle = RepositoryLifecycle(
        MemoryForge(), Ledger(tmp_path / "events.jsonl"), reap_after_hours=6, executor=executor
    )
    request = replace(
        _request(tmp_path),
        pre_staged=True,
        repository="acme/widgets",
        isolated_push=True,
    )

    assert lifecycle._commit_and_push(request) == ""

    push, environment = next(call for call in executor.calls if "push" in call[0])
    assert "origin" not in push
    assert "https://github.com/acme/widgets.git" in push
    assert "protocol.ext.allow=never" in push
    assert environment is not None and environment["GH_TOKEN"] == "publish-token"
    assert "TOUCHSTONE_STATE_KEY" not in environment


def test_node_builds_publication_from_project_configuration() -> None:
    context = SimpleNamespace(
        config=SimpleNamespace(
            forge=SimpleNamespace(
                slug="acme/widgets",
                default_branch="trunk",
                escalation_label="ops:review",
                required_workflows=("ci.yml",),
            ),
            timezone="UTC",
            git=GitConfig(author_name="Touchstone Bot", author_email="bot@example.com"),
        ),
        loop=lambda name: SimpleNamespace(
            name=name,
            label="automation:audit",
            auto_merge=False,
            auto_merge_strategy="squash",
            auto_merge_delete_branch=True,
            auto_merge_window=(),
            auto_merge_max_files=0,
        ),
    )
    state = {
        "loop": "architecture",
        "branch": "touchstone/run-2",
        "worktree": "/tmp/worktree",
        "risk": "low",
        "verdict": "approve",
        "verdict_reason": "covered by a focused regression test",
        "pre_staged": True,
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
    assert request.pre_staged is True
    assert (request.author_name, request.author_email) == (
        "Touchstone Bot",
        "bot@example.com",
    )


def test_a_successful_publication_records_the_branch_a_resume_will_verify(
    tmp_path: Path,
) -> None:
    """Approval compares the live pull request against the branch it was parked with."""
    repo = _worktree(tmp_path)
    ledger = Ledger(tmp_path / "events.jsonl")
    lifecycle = RepositoryLifecycle(
        MemoryForge(), ledger, reap_after_hours=6, executor=LocalExecutor()
    )

    result = lifecycle.publish(replace(_request(repo), risk="high", verdict="skipped"))
    projection = ledger.projection(result.finding_id)

    assert result.outcome == "awaiting_human"
    assert projection is not None
    assert projection.branch == result.branch != ""


def test_an_approved_publication_also_records_its_branch(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    ledger = Ledger(tmp_path / "events.jsonl")
    lifecycle = RepositoryLifecycle(
        MemoryForge(), ledger, reap_after_hours=6, executor=LocalExecutor()
    )

    result = lifecycle.publish(replace(_request(repo), risk="low", verdict="approve"))
    projection = ledger.projection(result.finding_id)

    assert result.outcome == "awaiting_checks"
    assert projection is not None
    assert projection.branch == result.branch != ""


def test_a_change_nobody_reviewed_says_why_nobody_did() -> None:
    """A `medium` change never reaches the reviewer: nothing it could answer
    would let the change merge unattended. That left an empty verdict rendering
    as `**skipped** —` with no reason, which is what a review that ran and
    failed looks like. Two pull requests were read that way, including by me.
    """
    from touchstone.lifecycle import _review_line

    def request(**kw):  # type: ignore[no-untyped-def]
        base = {"verdict": "", "review_reason": "", "risk": "low"}
        return SimpleNamespace(**{**base, **kw})

    judged = _review_line(request(verdict="reject", review_reason="R-NAM-4b", risk="low"))
    assert judged.startswith("**reject**")
    assert "R-NAM-4b" in judged

    unreviewed = _review_line(request(risk="medium"))
    assert "not reviewed" in unreviewed
    assert "medium" in unreviewed
    assert "skipped" not in unreviewed, "still indistinguishable from a failed review"

    failed = _review_line(request(risk="low", review_reason="the codex session failed"))
    assert "inconclusive" in failed
    assert "the codex session failed" in failed

    # A low-risk change with no verdict and no reason at all is the one case
    # where nothing can be said honestly, so it says that.
    silent = _review_line(request(risk="low"))
    assert "itself a defect" in silent
