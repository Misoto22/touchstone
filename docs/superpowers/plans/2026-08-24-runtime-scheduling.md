# Runtime Outcomes, Validation, and Durable Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce deterministic Validation Gates, expose canonical run/lifecycle state, and make every scheduler consume durable idempotent Due Slots.

**Architecture:** Validation is a secret-scrubbed executor boundary before publication. Run Outcome is separate from the append-only Change Lifecycle. A SQLite due store owns schedule generations, expiring claims, catch-up, attempts, and watermarks; launchd, systemd, and Actions are only Wake Signal adapters.

**Tech Stack:** Python 3.12+, `zoneinfo`, SQLite, dataclasses, LangGraph SQLite checkpoints, pytest

**Spec:** `docs/superpowers/specs/2026-08-24-stack-profiles-actions-design.md`

## Global Constraints

- Validation never inherits model, App, project, or service secrets.
- `status` is read-only; `reconcile` is the only status-adjacent mutating command.
- Existing `held`, `merging`, `escalated`, and `inconclusive` strings leave the machine contract.
- One repository admits one mutating claim at a time.
- Missed periods coalesce; they are never replayed one by one.
- Every production behavior starts with a failing test and ends with a focused commit.

---

### Task 1: Structured Validation Gates and secret-free preparation

**Files:**
- Create: `src/touchstone/validation.py`
- Modify: `src/touchstone/config_v2.py`
- Modify: `src/touchstone/runner.py`
- Modify: `src/touchstone/cli.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Produces: `ValidationCommand`, `ValidationResult`, `ValidationReport`
- Produces: `prepare(config: Config, targets: tuple[str, ...], executor: Executor) -> PreparationReport`
- Produces: `validate(config: Config, targets: tuple[str, ...], executor: Executor) -> ValidationReport`

- [ ] **Step 1: Write failing argv, timeout, secret, and mutation tests**

```python
def test_validation_scrubs_secret_like_environment(tmp_path: Path) -> None:
    command = gate(argv=(sys.executable, "-c", "import os; print(sorted(os.environ))"))
    result = run_gate(command, env={"OPENAI_API_KEY": "x", "PATH": os.environ["PATH"]})
    assert "OPENAI_API_KEY" not in result.stdout


def test_tracked_file_mutation_blocks_validation(git_repo: Path) -> None:
    command = gate(argv=(sys.executable, "-c", "open('tracked.txt','w').write('changed')"))
    report = validate_commands(git_repo, (command,), executor())
    assert report.outcome == "blocked"
    assert report.results[0].reason == "tracked-files-changed"
```

Also test no implicit shell, explicit shell acknowledgement, Target cwd confinement, command timeout, disabled candidates, locked package-manager preparation, Node lifecycle-script opt-in, and Python build-hook opt-in.

- [ ] **Step 2: Run tests and verify Validation Gate types are absent**

Run: `.venv/bin/python -m pytest tests/test_validation.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement the structured runner**

Use argv sequences with `shell=False`. Build a minimal environment allowlist (`PATH`, locale, temporary directory, package-manager cache paths) and remove secret-marker keys. Snapshot `git status --porcelain=v1 -z` before/after each Gate. Return typed results; do not raise for a test failure, but block the report.

- [ ] **Step 4: Insert preparation and validation before publish**

Preparation runs before model credentials are exposed by hosted jobs. The local runner validates only affected Targets after the reviewed change and before publication. A blocked report preserves diagnostics, sets exit 3, and never reaches publish nodes.

- [ ] **Step 5: Run validation, runner, and policy tests**

Run: `.venv/bin/python -m pytest tests/test_validation.py tests/test_runner_safety.py tests/test_policy.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Validation Gates**

```bash
git add src/touchstone/validation.py src/touchstone/config_v2.py src/touchstone/runner.py src/touchstone/cli.py tests/test_validation.py
git commit -m "feat: enforce structured validation gates"
```

### Task 2: Canonical Run Outcome and Change Lifecycle

**Files:**
- Create: `src/touchstone/outcomes.py`
- Modify: `src/touchstone/graph.py`
- Modify: `src/touchstone/ledger.py`
- Modify: `src/touchstone/lifecycle.py`
- Modify: `src/touchstone/status.py`
- Modify: `src/touchstone/runner.py`
- Modify: `src/touchstone/cli.py`
- Test: `tests/test_outcomes.py`
- Test: `tests/test_status.py`
- Test: `tests/test_resume.py`
- Test: `tests/test_publication.py`

**Interfaces:**
- Produces: `RunOutcome`, `ChangeState`, `RunResult`, `ResumeDecision`
- Produces: `RepositoryLifecycle.reconcile(...) -> ReconcileReport`
- Produces: pure `collect_status(...) -> StatusReport`

- [ ] **Step 1: Write failing mapping and purity tests**

```python
@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [("completed", 0), ("no_change", 0), ("rehearsed", 0), ("blocked", 3), ("failed", 1)],
)
def test_run_outcomes_have_stable_exit_codes(outcome: str, exit_code: int) -> None:
    assert RunResult(outcome=outcome).exit_code == exit_code


def test_status_never_calls_mutating_forge_methods(context: Context) -> None:
    collect_status(config(), context, scheduler=None)
    assert context.forge.mutations == []
```

Test engine/contract exhaustion→failed, Gate/health/permission→blocked, risk/reviewer reject→completed+awaiting_human, PR open→completed+awaiting_checks, actual merge only after reconcile, close→closed, partial publication fields, and cleanup failure visibility.

- [ ] **Step 2: Run focused tests and observe legacy outcome failures**

Run: `.venv/bin/python -m pytest tests/test_outcomes.py tests/test_status.py tests/test_resume.py tests/test_publication.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement canonical types and compatibility projection**

```python
class RunOutcome(StrEnum):
    COMPLETED = "completed"
    NO_CHANGE = "no_change"
    BLOCKED = "blocked"
    FAILED = "failed"
    REHEARSED = "rehearsed"


class ChangeState(StrEnum):
    PROPOSED = "proposed"
    AWAITING_HUMAN = "awaiting_human"
    AWAITING_CHECKS = "awaiting_checks"
    MERGED = "merged"
    CLOSED = "closed"
    REAPED = "reaped"
    FAILED = "failed"
```

Map legacy ledger rows on read but write only canonical states. `RunResult` carries lifecycle, reason code, detail, PR URL/number, candidate ID, partial flag, and retryability.

- [ ] **Step 4: Separate status and reconcile commands**

`touchstone status` reads ledger/events/scheduler files without Forge mutation. `touchstone reconcile` performs live GitHub lookup, updates lifecycle, reports partial/inconclusive lookup errors, and returns nonzero when reconciliation itself fails.

- [ ] **Step 5: Replace resume inputs and preserve partial writes**

Accept only `approve`, `close`, `reanalyze`. Verify candidate, reviewed SHA, lineage, and live PR head. Approve marks a draft ready and enters awaiting-checks without enabling auto-merge by default. Partial publication preserves remote branch/PR IDs and routes the next operation through reconcile.

- [ ] **Step 6: Run lifecycle and acceptance tests**

Run: `.venv/bin/python -m pytest tests/test_outcomes.py tests/test_ledger.py tests/test_lifecycle.py tests/test_status.py tests/test_resume.py tests/test_publication.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 7: Commit canonical outcomes**

```bash
git add src/touchstone/outcomes.py src/touchstone/graph.py src/touchstone/ledger.py src/touchstone/lifecycle.py src/touchstone/status.py src/touchstone/runner.py src/touchstone/cli.py tests/test_outcomes.py tests/test_status.py tests/test_resume.py tests/test_publication.py
git commit -m "feat: separate run and change state"
```

### Task 3: Due Slot model, SQLite claims, and catch-up

**Files:**
- Create: `src/touchstone/scheduling/due.py`
- Create: `src/touchstone/scheduling/store.py`
- Modify: `src/touchstone/scheduling/model.py`
- Test: `tests/test_due.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Produces: `Schedule.next_after(instant: datetime, timezone: ZoneInfo) -> datetime`
- Produces: `DueStore.claim(slot: DueSlot, owner: str, now: datetime, ttl: timedelta) -> ClaimResult`
- Produces: `DueEvaluator.evaluate(config: Config, now: datetime) -> tuple[DueLoop, ...]`
- Produces: `DueStore.finish(claim: DurableClaim, result: RunResult, now: datetime) -> None`

- [ ] **Step 1: Write failing time, DST, claim, and retry tests**

```python
def test_due_slot_identity_includes_schedule_generation() -> None:
    first = due_slot("code", schedule="hourly@00", scheduled_for=UTC_NOON)
    changed = due_slot("code", schedule="hourly@30", scheduled_for=UTC_NOON)
    assert first.id != changed.id


def test_expired_claim_can_be_reacquired(store: DueStore) -> None:
    slot = slot_at(UTC_NOON)
    first = store.claim(slot, owner="one", now=UTC_NOON, ttl=timedelta(minutes=5))
    second = store.claim(slot, owner="two", now=UTC_NOON + timedelta(minutes=6), ttl=timedelta(minutes=5))
    assert first.acquired and second.acquired
```

Test `hourly@MM`, daily/weekly, IANA zone conversion, nonexistent DST time shifted forward, repeated local time once, manual-only, coalesced missed count, immediate Clean Start, schedule-generation reset, stable priority/ID order, blocked consumption, failure retry with default three total attempts, terminal failure advance, and Partial Failure reconcile-only.

- [ ] **Step 2: Run tests and confirm the current parser lacks anchors/state**

Run: `.venv/bin/python -m pytest tests/test_schedule.py tests/test_due.py -q`

Expected: FAIL.

- [ ] **Step 3: Extend schedule parsing and generation hashing**

Use strict full-match parsing. Compute generation from normalized Loop ID, cadence, timezone, and relevant due-policy configuration. Return aware UTC datetimes. Store local-time identity to suppress the duplicated fall-back hour.

- [ ] **Step 4: Implement transactional SQLite storage**

Create tables for schedule generations, slots, claims, attempts, and watermarks with schema versioning. Use `BEGIN IMMEDIATE` for claim/finish. A claim records owner and expiry. A durable finish records outcome, snapshot identity, attempt count, consumed time, reason, and next retry.

- [ ] **Step 5: Implement catch-up and retry evaluation**

Coalesce all overdue periods into the latest due timestamp and record missed count/lateness. Failed slots reuse the same identity across wakes with configured exponential delay and jitter; after the configured total attempts, mark terminal failure and move to the next cadence.

- [ ] **Step 6: Run due and event tests**

Run: `.venv/bin/python -m pytest tests/test_schedule.py tests/test_due.py tests/test_events.py -q`

Expected: PASS.

- [ ] **Step 7: Commit durable scheduling state**

```bash
git add src/touchstone/scheduling/model.py src/touchstone/scheduling/due.py src/touchstone/scheduling/store.py tests/test_schedule.py tests/test_due.py
git commit -m "feat: claim durable due slots"
```

### Task 4: Unified run-due and Wake Signal adapters

**Files:**
- Modify: `src/touchstone/scheduling/base.py`
- Modify: `src/touchstone/scheduling/launchd.py`
- Modify: `src/touchstone/scheduling/systemd.py`
- Modify: `src/touchstone/runner.py`
- Modify: `src/touchstone/cli.py`
- Modify: `src/touchstone/doctor.py`
- Test: `tests/test_run_due.py`
- Test: `tests/test_scheduling.py`

**Interfaces:**
- Produces: `run_due(config: Config, *, now: datetime, loop: str | None, force: bool) -> RunDueReport`
- Changes: every native scheduler invokes `touchstone run-due`, never `touchstone run LOOP`

- [ ] **Step 1: Write failing adapter-parity and sequencing tests**

```python
def test_native_adapters_install_one_wake_per_config(tmp_path: Path) -> None:
    launchd = render_launchd(config_with_three_loops(), tmp_path)
    systemd = render_systemd(config_with_three_loops(), tmp_path)
    assert launchd.command[-1] == "run-due"
    assert systemd.command[-1] == "run-due"
    assert len(launchd.files) == 1
    assert len(systemd.timer_files) == 1


def test_active_change_stops_remaining_due_loops(harness: DueHarness) -> None:
    report = harness.run(outcomes={"a": "no_change", "b": "completed-awaiting_checks"})
    assert report.started == ("a", "b")
    assert report.remaining_due == ("c",)
```

- [ ] **Step 2: Run tests and confirm adapters still invoke individual Loops**

Run: `.venv/bin/python -m pytest tests/test_run_due.py tests/test_scheduling.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement run-due orchestration**

Evaluate due Loops, acquire the repository writer boundary and a per-slot claim, execute by priority/ID, persist finish before releasing the claim, and stop on active change or repository-wide blocked/failed. `--force` creates a manual slot for one Loop but passes through every Gate.

- [ ] **Step 4: Render one Wake Signal for launchd and systemd**

Render deterministic, credential-free units that call the same config and `run-due`. Preserve dry-run output. Migration removes only Touchstone-owned old per-Loop unit files after the new unit is successfully installed.

- [ ] **Step 5: Add doctor checks and full scheduler tests**

Doctor reports timezone, next due, last slot, claim age/owner, retries, lateness, missed count, installed/enabled Wake Signal state, and stale legacy timers. Run `.venv/bin/python -m pytest tests/test_run_due.py tests/test_scheduling.py tests/test_doctor.py tests/test_acceptance.py -q`.

Expected: PASS.

- [ ] **Step 6: Commit unified Wake Signals**

```bash
git add src/touchstone/scheduling src/touchstone/runner.py src/touchstone/cli.py src/touchstone/doctor.py tests/test_run_due.py tests/test_scheduling.py tests/test_doctor.py
git commit -m "feat: unify scheduled loop dispatch"
```
