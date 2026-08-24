# Repository Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GitHub publication, reconciliation, reaping, and human resume reflect live pull-request truth and fail safely.

**Architecture:** An append-only ledger projects finding state, while `RepositoryLifecycle` is the deep module that reconciles that projection with GitHub. Graph nodes request publication outcomes and never infer success from an unchecked `gh` call.

**Tech Stack:** Python 3.12+, dataclasses, JSONL, GitHub CLI JSON, LangGraph SQLite checkpoints, pytest

**Spec:** `docs/superpowers/specs/2026-08-24-ready-to-use-design.md`

## Global Constraints

- No failed auto-merge is recorded as armed or merging.
- Only armed, parked, and merged findings suppress rediscovery.
- Failed, reaped, and closed findings remain auditable and rediscoverable.
- Resume may act only on the exact independently reviewed head SHA.
- Draft escalations are never automatically reaped.
- Invalid agent output is inconclusive rather than clean.
- Every production behavior is introduced through a failing test first.

---

### Task 1: Append-only lifecycle events and projections

**Files:**
- Modify: `src/touchstone/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces: `Ledger.append(event: LifecycleEvent) -> None`
- Produces: `Ledger.projections() -> dict[str, FindingProjection]`
- Produces: `Ledger.suppressed_titles() -> list[str]`
- Preserves: reading legacy status rows

- [ ] **Step 1: Write failing projection tests**

```python
@pytest.mark.parametrize("terminal", ["failed", "reaped", "closed"])
def test_terminal_failure_does_not_suppress_rediscovery(tmp_path: Path, terminal: str) -> None:
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(event("f-1", "armed", title="Broken invariant"))
    ledger.append(event("f-1", terminal, title="Broken invariant"))
    assert ledger.suppressed_titles() == []


def test_legacy_merging_row_projects_as_armed(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path, {"status": "merging", "title": "Old finding", "pr": 7})
    assert Ledger(path).projections()[legacy_id("Old finding")].state == "armed"
```

- [ ] **Step 2: Run tests and confirm current status allowlisting fails the transition cases**

Run: `.venv/bin/python -m pytest tests/test_ledger.py -q`

Expected: FAIL because the current ledger has no events or projections.

- [ ] **Step 3: Implement events and last-event projection**

```python
@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    finding_id: str
    state: Literal["proposed", "armed", "parked", "merged", "failed", "reaped", "closed"]
    title: str
    loop: str
    pr: int | None = None
    head_sha: str | None = None
    detail: str = ""
```

Generate stable IDs from `sha256(f"{loop}\0{normalized_title}")[:16]`. Skip truncated JSONL rows. Project in append order. Keep a compatibility `record(**fields)` adapter until callers migrate.

- [ ] **Step 4: Run ledger and acceptance tests**

Run: `.venv/bin/python -m pytest tests/test_ledger.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 5: Commit lifecycle events**

```bash
git add src/touchstone/ledger.py tests/test_ledger.py tests/test_acceptance.py
git commit -m "feat: project finding lifecycle events"
```

### Task 2: Typed live pull-request state

**Files:**
- Modify: `src/touchstone/forge.py`
- Test: `tests/test_forge.py`
- Test support: `tests/support/fake_gh.py`

**Interfaces:**
- Produces: `Forge.pull(number: int) -> PullState | None`
- Produces: `Forge.pull_for_branch(branch: str) -> PullState | None`
- Produces: `Forge.mark_ready(number: int) -> OperationResult`
- Changes: all forge mutations return `OperationResult`, never bare booleans

- [ ] **Step 1: Write failing tests against a stateful fake `gh` executable**

```python
def test_pull_state_preserves_head_and_check_conclusions(fake_gh: FakeGh, forge: Forge) -> None:
    fake_gh.add_pull(number=12, head_sha="abc123", draft=True, checks=["SUCCESS", "FAILURE"])
    pull = forge.pull(12)
    assert pull == PullState(
        number=12,
        head_sha="abc123",
        draft=True,
        check_state="failure",
        merged_at=None,
        closed=False,
    )


def test_auto_merge_failure_returns_the_error(fake_gh: FakeGh, forge: Forge) -> None:
    fake_gh.fail_next("pr merge", stderr="auto-merge is disabled")
    result = forge.arm_auto_merge(12)
    assert (result.ok, result.detail) == (False, "auto-merge is disabled")
```

- [ ] **Step 2: Run tests and confirm Forge lacks typed state and result details**

Run: `.venv/bin/python -m pytest tests/test_forge.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement exact GitHub JSON parsing and operation results**

```python
@dataclass(frozen=True, slots=True)
class OperationResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PullState:
    number: int
    head_sha: str
    draft: bool
    check_state: Literal["success", "failure", "pending", "unknown"]
    merged_at: str | None
    closed: bool
    created_at: str
    url: str
```

Use `gh pr view --json number,headRefOid,isDraft,state,mergedAt,createdAt,url,statusCheckRollup`. Treat any failure conclusion as failure, all successful as success, empty/in-progress as pending or unknown.

- [ ] **Step 4: Run forge tests and existing acceptance tests**

Run: `.venv/bin/python -m pytest tests/test_forge.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the typed forge adapter**

```bash
git add src/touchstone/forge.py tests/test_forge.py tests/support/fake_gh.py
git commit -m "feat: model live pull request state"
```

### Task 3: Reconciliation and reaping

**Files:**
- Create: `src/touchstone/lifecycle.py`
- Modify: `src/touchstone/runner.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Produces: `RepositoryLifecycle.reconcile(loop: LoopConfig, now: datetime) -> ReconcileReport`
- Consumes: typed `Forge` and projected `Ledger`

- [ ] **Step 1: Write failing reconciliation tests**

```python
def test_reconcile_records_merged_when_github_has_merged_pull(
    lifecycle: RepositoryLifecycle,
) -> None:
    seed(lifecycle.ledger, state="armed", pr=12, head_sha="abc")
    lifecycle.forge.set_pull(12, merged_at="2026-08-24T01:00:00Z")
    lifecycle.reconcile(loop("code"), now=NOW)
    assert lifecycle.ledger.projection("f-1").state == "merged"


def test_reaper_closes_only_old_failed_non_drafts(lifecycle: RepositoryLifecycle) -> None:
    seed_old_armed_pull(lifecycle, pr=12, age_hours=7, check_state="failure", draft=False)
    report = lifecycle.reconcile(loop("code", reap_after_hours=6), now=NOW)
    assert report.reaped == (12,)
    assert lifecycle.ledger.projection("f-1").state == "reaped"


def test_reaper_never_closes_parked_drafts(lifecycle: RepositoryLifecycle) -> None:
    seed_old_parked_pull(lifecycle, pr=13, age_days=90)
    assert lifecycle.reconcile(loop("code"), now=NOW).reaped == ()
```

- [ ] **Step 2: Run tests and confirm the lifecycle module is absent**

Run: `.venv/bin/python -m pytest tests/test_lifecycle.py -q`

Expected: FAIL importing `touchstone.lifecycle`.

- [ ] **Step 3: Implement projection-to-live-state reconciliation**

Reconcile all nonterminal projections for the loop. Record merged/closed/failed transitions, and reap only conclusively failed, non-draft, expired automated pull requests. If GitHub lookup fails, report inconclusive and mutate nothing.

- [ ] **Step 4: Invoke reconciliation before gates and expose its report to status**

`runner.execute` calls reconciliation after acquiring the lock and before checking the slot. Gate decisions therefore operate on current projections.

- [ ] **Step 5: Run lifecycle, runner, and acceptance tests**

Run: `.venv/bin/python -m pytest tests/test_lifecycle.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit reconciliation and reaping**

```bash
git add src/touchstone/lifecycle.py src/touchstone/runner.py tests/test_lifecycle.py
git commit -m "feat: reconcile automated pull requests"
```

### Task 4: Safe publication and idempotent retry

**Files:**
- Modify: `src/touchstone/lifecycle.py`
- Modify: `src/touchstone/nodes/publish.py`
- Modify: `src/touchstone/graph.py`
- Test: `tests/test_publication.py`

**Interfaces:**
- Produces: `RepositoryLifecycle.publish(request: PublicationRequest) -> PublicationResult`
- Replaces unchecked `_commit_and_push`, `_open`, `merge`, and `park` orchestration in graph nodes

- [ ] **Step 1: Write failing publication tests**

```python
def test_failed_auto_merge_is_held_and_not_armed(lifecycle: RepositoryLifecycle) -> None:
    lifecycle.forge.fail_auto_merge("auto-merge disabled")
    result = lifecycle.publish(low_risk_request())
    assert result.outcome == "held"
    assert lifecycle.ledger.projection(result.finding_id).state == "proposed"


def test_retry_reuses_existing_pull_for_branch(lifecycle: RepositoryLifecycle) -> None:
    lifecycle.forge.add_pull(number=12, branch="touchstone/run-1", head_sha="abc")
    result = lifecycle.publish(request(branch="touchstone/run-1", head_sha="abc"))
    assert result.pr == 12
    assert lifecycle.forge.created_pull_count == 0
```

- [ ] **Step 2: Run tests and confirm current nodes misreport merge arming**

Run: `.venv/bin/python -m pytest tests/test_publication.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement publication as one lifecycle operation**

```python
@dataclass(frozen=True, slots=True)
class PublicationRequest:
    finding_id: str
    loop: str
    branch: str
    worktree: Path
    reviewed_head_sha: str
    risk: Risk
    verdict: Verdict
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class PublicationResult:
    outcome: Literal["armed", "parked", "held"]
    finding_id: str
    pr: int | None
    detail: str
```

Commit using configured or inherited Git identity, with no personal author or vendor co-author. Reuse an existing branch pull on retry. Persist `armed` only after GitHub accepts auto-merge; persist `parked` only after draft conversion and escalation label both succeed.

- [ ] **Step 4: Adapt graph nodes to map lifecycle results into LoopState**

Keep the publish-before-interrupt split. Graph state stores finding ID, PR, and reviewed head SHA for resume.

- [ ] **Step 5: Run publication and graph regression tests**

Run: `.venv/bin/python -m pytest tests/test_publication.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit safe publication**

```bash
git add src/touchstone/lifecycle.py src/touchstone/nodes/publish.py src/touchstone/graph.py tests/test_publication.py tests/test_acceptance.py
git commit -m "fix: publish from verified lifecycle state"
```

### Task 5: Safe human resume

**Files:**
- Modify: `src/touchstone/lifecycle.py`
- Modify: `src/touchstone/runner.py`
- Modify: `src/touchstone/nodes/publish.py`
- Test: `tests/test_resume.py`

**Interfaces:**
- Produces: `RepositoryLifecycle.resume(request: ResumeRequest) -> PublicationResult`
- Consumes: checkpoint PR and reviewed head SHA

- [ ] **Step 1: Write failing resume tests**

```python
def test_resume_refuses_a_changed_head(lifecycle: RepositoryLifecycle) -> None:
    parked(lifecycle, pr=12, reviewed_head_sha="reviewed")
    lifecycle.forge.set_head(12, "changed")
    result = lifecycle.resume(resume_request(pr=12, decision="merge"))
    assert result.outcome == "held"
    assert "changed since review" in result.detail


def test_resume_marks_draft_ready_before_arming(lifecycle: RepositoryLifecycle) -> None:
    parked(lifecycle, pr=12, reviewed_head_sha="same", live_head_sha="same")
    result = lifecycle.resume(resume_request(pr=12, decision="merge"))
    assert result.outcome == "armed"
    assert lifecycle.forge.transitions == ["ready:12", "auto-merge:12"]
```

- [ ] **Step 2: Run tests and confirm current resume lacks validation and ready transition**

Run: `.venv/bin/python -m pytest tests/test_resume.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement locked resume with health and SHA checks**

Acquire the shared lock, reconcile the PR, rerun configured health gates, compare the live head SHA, mark the PR ready, and arm auto-merge. A close decision closes and records `closed`. Any failed operation returns held without claiming a later state.

- [ ] **Step 4: Route graph resume through the lifecycle module without reopening the PR**

Preserve LangGraph checkpoint continuity and the regression that publish occurs exactly once.

- [ ] **Step 5: Run resume and full graph tests**

Run: `.venv/bin/python -m pytest tests/test_resume.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit safe resume**

```bash
git add src/touchstone/lifecycle.py src/touchstone/runner.py src/touchstone/nodes/publish.py tests/test_resume.py tests/test_acceptance.py
git commit -m "fix: bind resume to the reviewed pull head"
```

### Task 6: Validated agent contracts and structured run events

**Files:**
- Create: `src/touchstone/events.py`
- Modify: `src/touchstone/nodes/audit.py`
- Modify: `src/touchstone/nodes/review.py`
- Modify: `src/touchstone/runner.py`
- Test: `tests/test_agent_contracts.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces: `parse_finding(raw: str) -> Finding`
- Produces: `EventLog.append(event: RunEvent) -> None`

- [ ] **Step 1: Write failing malformed-output and redaction tests**

```python
@pytest.mark.parametrize("raw", ["", "not json", '{"status":"unexpected"}'])
def test_invalid_finding_is_inconclusive(raw: str) -> None:
    assert parse_finding(raw).status == "inconclusive"


def test_event_log_excludes_prompts_and_environment_values(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(run_event(config=fake_config(secret_env="do-not-log")))
    text = (tmp_path / "events.jsonl").read_text()
    assert "do-not-log" not in text
    assert "prompt" not in text
```

- [ ] **Step 2: Run tests and confirm invalid JSON is currently treated as clean**

Run: `.venv/bin/python -m pytest tests/test_agent_contracts.py tests/test_events.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement strict schemas and run event JSONL**

Validate status-specific required fields. Record normalized config fingerprint, engine/model/effort, gate results, durations, costs, risk transitions, PR, and outcome. Never record prompt text, environment values, credentials, or complete command lines.

- [ ] **Step 4: Add `touchstone status [--json]` using lifecycle and event projections**

Status runs reconciliation first and renders current slots, parked decisions, armed pulls, terminal failures, last runs, and scheduler status.

- [ ] **Step 5: Run all lifecycle tests, lint, and graph check**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m touchstone.cli graph --check`

Expected: all commands exit 0.

- [ ] **Step 6: Commit validated contracts and events**

```bash
git add src/touchstone tests
git commit -m "feat: validate run contracts and outcomes"
```
