"""What happens around the graph.

The gates, the lock and the worktree live here rather than as nodes. Three of
the gates are "if this, the whole run stops", which is an early return and not
a branch worth drawing; the lock and the worktree are filesystem semantics, and
wrapping them in nodes would produce a harder-to-debug equivalent of an `if`.

What the graph gets is the part with branches worth seeing.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from touchstone.config import Config, LoopConfig
from touchstone.events import EventLog, run_event
from touchstone.graph import build
from touchstone.nodes.context import configure, current
from touchstone.outcomes import ChangeState, RunOutcome, RunResult, from_legacy_outcome


class Held(Exception):
    """A gate said no. Not an error: the loop declining to start is it working."""


def _lock(state_dir: Path) -> Path:
    """A directory, because it is the only primitive atomic everywhere.

    Shared between the loops on purpose, which serialises them — the harness
    review is meant to run after the code slot releases, and two sessions
    editing one worktree would corrupt each other anyway.
    """
    lock = state_dir / "lock"
    try:
        lock.mkdir(parents=True)
    except FileExistsError:
        holder = (lock / "pid").read_text().strip() if (lock / "pid").exists() else "0"
        if holder.isdigit() and int(holder) > 0 and _alive(int(holder)):
            raise Held(f"another run holds the lock (pid {holder})") from None
        # rm, not rmdir: the directory holds the pid file, so rmdir fails and
        # the lock is never released.
        shutil.rmtree(lock, ignore_errors=True)
        lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    return lock


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _gates(config: Config, loop_name: str, *, dry_run: bool) -> None:
    context = current()
    loop = context.loop(loop_name)

    # The kill switch holds even for a rehearsal. It is the one gate that means
    # a person said stop, rather than a condition about publishing.
    paused = Path(config.state_dir) / "PAUSED"
    if paused.exists():
        raise Held(f"paused: {paused.read_text().strip()}")

    _partial_write_gate(config)

    if dry_run:
        # Everything below is about publishing, and a rehearsal publishes
        # nothing. Both gates were checked first once, and the effect was that
        # a rehearsal could not run while any pull request was open — which,
        # for a loop whose job is opening pull requests, is nearly always.
        return

    # One harness review open at a time means open, not open-and-not-a-draft.
    # For the code audit the opposite is right: if drafts held the slot, the
    # first medium-risk finding would be the last thing the loop ever did.
    include_drafts = bool(loop.require_change_under)
    held = context.forge.open_pulls(loop.label, include_drafts=include_drafts)
    if held is None:
        raise Held("could not verify the open pull request slot")
    if held:
        raise Held(f"slot held by #{held[0]['number']}: {held[0].get('url', '')}")

    _health_gate(config)
    _publication_gate(config, loop)


def _partial_write_gate(config: Config) -> None:
    from touchstone.ledger import Ledger

    unresolved = [
        projection
        for projection in Ledger(Path(config.state_dir) / "ledger.jsonl").projections().values()
        if projection.partial and projection.state == ChangeState.FAILED
    ]
    if unresolved:
        identity = unresolved[0].branch or unresolved[0].finding_id
        raise Held(
            f"partial remote publication requires reconciliation before new analysis: {identity}"
        )


def _health_gate(config: Config) -> None:
    """Require explicit success from every project-configured workflow."""
    if not config.forge.required_workflows:
        raise Held("no required workflows are configured for unattended publication")
    forge = current().forge
    unhealthy: list[str] = []
    for workflow in config.forge.required_workflows:
        conclusion = forge.latest_run(workflow, branch=config.forge.default_branch)
        if conclusion != "success":
            unhealthy.append(f"{workflow}={conclusion}")
    if unhealthy:
        raise Held(f"production not known good: {' '.join(unhealthy)}")


def _publication_gate(config: Config, loop: LoopConfig) -> None:
    """Refuse to buy an author session when GitHub cannot accept its result."""
    forge = current().forge
    repository = forge.repository_info()
    if repository is None:
        raise Held("GitHub repository is not accessible; run touchstone doctor")
    expected = {loop.label, config.forge.escalation_label}
    missing = sorted(expected - forge.labels())
    if missing:
        raise Held(
            f"configured GitHub labels are missing: {', '.join(missing)}; run touchstone setup"
        )


def _worktree(config: Config) -> tuple[str, str]:
    context = current()
    branch = f"audit/{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}"
    path = config.execution_worktree
    repo = config.execution_repo
    base = f"origin/{config.forge.default_branch}"

    prepared = context.executor.run(["mkdir", "-p", str(Path(path).parent)], timeout=60)
    if not prepared.ok:
        raise Held(f"could not prepare worktree state: {prepared.tail()}")
    fetched = context.executor.run(
        ["git", "-C", repo, "fetch", "--prune", "--quiet", "origin"], timeout=300
    )
    if not fetched.ok:
        raise Held(f"could not fetch the current default branch: {fetched.tail()}")
    pruned = context.executor.run(["git", "-C", repo, "worktree", "prune"], timeout=60)
    if not pruned.ok:
        raise Held(f"could not inspect existing worktrees: {pruned.tail()}")
    context.executor.run(["git", "-C", repo, "worktree", "remove", "--force", path], timeout=60)
    # Always from the fetched base, never from whatever the clone has checked
    # out: a clone parked on a feature branch would seed every branch weeks
    # behind, and that clone is where a person works, so it usually is.
    result = context.executor.run(
        ["git", "-C", repo, "worktree", "add", "-b", branch, path, base], timeout=180
    )
    if not result.ok:
        raise Held(f"could not create a worktree: {result.tail()}")
    return (path, branch)


def _teardown(config: Config, path: str, branch: str, *, published: bool) -> tuple[str, ...]:
    context = current()
    repo = config.execution_repo
    operations = [
        (
            ["git", "-C", repo, "worktree", "remove", "--force", path],
            "remove the temporary worktree",
        ),
        (["git", "-C", repo, "worktree", "prune"], "prune worktree metadata"),
    ]
    if not published:
        # Removing a worktree leaves its branch. A run that published wants it;
        # a clean pass or a crash leaves a dead ref nothing collects, and at one
        # run an hour that is a branch list nobody can read within a week.
        operations.append((["git", "-C", repo, "branch", "-D", branch], "delete the run branch"))
    errors: list[str] = []
    for argv, action in operations:
        result = context.executor.run(argv, timeout=60)
        if not result.ok:
            errors.append(f"could not {action}: {result.tail()}")
    return tuple(errors)


def execute(config: Config, *, loop: str, dry_run: bool = False) -> int:
    state_dir = Path(config.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    configure(config)
    event_log = EventLog(state_dir / "events.jsonl")
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    event_log.append(run_event(config, run_id=run_id, kind="started", loop=loop))
    final_outcome = RunOutcome.FAILED.value
    final_detail = ""
    final_cost: float | None = None
    final_risk = ""
    final_verdict = ""
    final_pr: int | None = None

    try:
        lock = _lock(state_dir)
    except Held as held:
        print(held)
        event_log.append(
            run_event(
                config,
                run_id=run_id,
                kind="finished",
                loop=loop,
                outcome=RunOutcome.BLOCKED.value,
                duration_seconds=time.monotonic() - started,
                detail=str(held),
            )
        )
        return 3

    path = branch = ""
    published = False
    try:
        _gates(config, loop, dry_run=dry_run)
        path, branch = _worktree(config)
        thread_id = f"{loop}-{branch}"
        with SqliteSaver.from_conn_string(str(state_dir / "checkpoints.sqlite")) as saver:
            app = build().compile(checkpointer=saver)
            thread = {"configurable": {"thread_id": thread_id}}
            final = app.invoke(
                {"loop": loop, "worktree": path, "branch": branch, "dry_run": dry_run}, thread
            )
            # An interrupt returns the state as it stood *before* the node
            # returned, because the node did not return — it stopped. Reading
            # `outcome` from it therefore gets the default, which is how one run
            # reported `clean` while its pull request sat open. Ask the graph
            # whether it is paused instead of inferring it from the payload.
            paused = bool(app.get_state(thread).next)

        # The nodes keep their own ledger rows: they are the only ones that know
        # what actually reached the forge. Recording again here produced two
        # rows for one run, disagreeing with each other.
        outcome = str(final.get("outcome") or ("awaiting_human" if paused else "clean"))
        final_pr = int(final["pr"]) if final.get("pr") is not None else None
        final_risk = str(final.get("risk") or "")
        final_verdict = str(final.get("verdict") or "")
        reported_costs = [value for value in final.get("cost", []) if value is not None]
        final_cost = sum(reported_costs) if reported_costs else None
        final_detail = "; ".join(str(note) for note in final.get("notes", []))
        result = from_legacy_outcome(
            outcome,
            dry_run=dry_run,
            paused=paused,
            pr=final_pr,
            detail=final_detail,
            partial=final.get("partial") is True,
        )
        final_outcome = result.outcome.value
        published = result.lifecycle is not None
        lifecycle = f" / {result.lifecycle.value}" if result.lifecycle is not None else ""
        print(
            f"{loop}: {result.outcome.value}{lifecycle}"
            + (f" #{final['pr']}" if final.get("pr") else "")
        )
        for note in final.get("notes", []):
            print(f"  {note}")
        if paused:
            parked_head = str(final.get("reviewed_head_sha") or "")
            if parked_head:
                print(f"  parked head: {parked_head}")
            print(f"  parked; resume with: touchstone resume {thread_id} approve|close|reanalyze")
        return result.exit_code
    except Held as held:
        print(held)
        final_detail = str(held)
        final_outcome = RunOutcome.BLOCKED.value
        return RunResult(
            RunOutcome.BLOCKED,
            reason_code="safety-gate",
            detail=str(held),
        ).exit_code
    finally:
        if path:
            cleanup_errors = _teardown(config, path, branch, published=published)
            for error in cleanup_errors:
                print(f"warning: {error}", file=sys.stderr)
            if cleanup_errors:
                final_detail = "; ".join(filter(None, (final_detail, *cleanup_errors)))
        shutil.rmtree(lock, ignore_errors=True)
        event_log.append(
            run_event(
                config,
                run_id=run_id,
                kind="finished",
                loop=loop,
                outcome=final_outcome,
                duration_seconds=time.monotonic() - started,
                cost=final_cost,
                risk_to=final_risk,
                verdict=final_verdict,
                pr=final_pr,
                detail=final_detail,
            )
        )


def resume(config: Config, *, thread: str, answer: str) -> int:
    """Continue a parked thread from where it stopped.

    The whole reason `park` is an interrupt rather than an exit: a person's
    answer reaches the run that asked, instead of the next hour starting over
    and paying for another audit to reach the same conclusion.
    """
    from langgraph.types import Command

    configure(config)
    state_dir = Path(config.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock = _lock(state_dir)
    except Held as held:
        print(held)
        return 0

    reanalysis_loop = ""
    result_code = 0
    try:
        with SqliteSaver.from_conn_string(str(state_dir / "checkpoints.sqlite")) as saver:
            app = build().compile(checkpointer=saver)
            graph_config = {"configurable": {"thread_id": thread}}
            checkpoint = app.get_state(graph_config)
            values = checkpoint.values
            if not checkpoint.next or not values:
                raise Held(f"thread {thread!r} is not waiting for an operator decision")
            loop_name = str(values.get("loop") or "")
            if not loop_name:
                raise Held(f"thread {thread!r} has no loop identity")

            if answer == "approve":
                _health_gate(config)
                _publication_gate(config, config.loop(loop_name))
            final = app.invoke(Command(resume=answer), graph_config)
        print(f"{thread}: {final.get('outcome', 'unknown')}")
        for note in final.get("notes", []):
            print(f"  {note}")
        if final.get("outcome") == "reanalyze":
            reanalysis_loop = loop_name
    except Held as held:
        print(held)
        result_code = 3
    finally:
        shutil.rmtree(lock, ignore_errors=True)
    if reanalysis_loop:
        print(f"{thread}: starting fresh analysis from the current default branch")
        return execute(config, loop=reanalysis_loop)
    return result_code


@dataclass(frozen=True, slots=True)
class RunDueReport:
    started: tuple[str, ...]
    results: tuple[RunResult, ...]
    remaining_due: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        if any(result.outcome == RunOutcome.BLOCKED for result in self.results):
            return 3
        if any(result.outcome == RunOutcome.FAILED for result in self.results):
            return 1
        return 0


def run_due(
    config: Config,
    *,
    now: dt.datetime,
    loop: str | None,
    force: bool,
    execute_loop: Callable[[Config, str], RunResult] | None = None,
) -> RunDueReport:
    from touchstone.scheduling.due import DueEvaluator, DueLoop, DueSlot
    from touchstone.scheduling.store import DueStore

    if now.tzinfo is None:
        raise ValueError("run-due requires an aware datetime")
    if force and loop is None:
        raise ValueError("--force requires --loop")
    if loop is not None and loop not in config.loops:
        raise ValueError(f"unknown Loop {loop!r}")
    state_dir = Path(config.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    store = DueStore(state_dir / "due.sqlite")
    current = now.astimezone(dt.UTC).replace(microsecond=0)
    if force:
        assert loop is not None
        configured = config.loops[loop]
        due = (
            DueLoop(
                DueSlot(loop, "manual", current, manual=True),
                0,
                dt.timedelta(),
                configured.priority,
            ),
        )
    else:
        due = DueEvaluator(store).evaluate(config, current)
        if loop is not None:
            due = tuple(item for item in due if item.slot.loop_id == loop)

    runner = execute_loop or _execute_loop_result
    owner = f"{os.getpid()}:{uuid.uuid4().hex}"
    started: list[str] = []
    results: list[RunResult] = []
    remaining = [item.slot.loop_id for item in due]
    for item in due:
        claimed = store.claim(
            item.slot,
            owner=owner,
            now=current,
            ttl=dt.timedelta(hours=2),
            missed_count=item.missed_count,
        )
        if not claimed.acquired or claimed.claim is None:
            continue
        name = item.slot.loop_id
        started.append(name)
        remaining.remove(name)
        try:
            result = runner(config, name)
        except Exception as exc:
            result = RunResult(
                RunOutcome.FAILED,
                reason_code="runner-exception",
                detail=type(exc).__name__,
                retryable=True,
            )
        store.finish(
            claimed.claim,
            result,
            now=current,
            snapshot=f"run:{item.slot.id}",
        )
        results.append(result)
        active = result.lifecycle in {
            ChangeState.PROPOSED,
            ChangeState.AWAITING_HUMAN,
            ChangeState.AWAITING_CHECKS,
        }
        if active or result.outcome in {RunOutcome.BLOCKED, RunOutcome.FAILED}:
            break
    return RunDueReport(tuple(started), tuple(results), tuple(remaining))


def _execute_loop_result(config: Config, loop: str) -> RunResult:
    code = execute(config, loop=loop)
    finished = [
        row
        for row in EventLog(Path(config.state_dir) / "events.jsonl").rows()
        if row.get("kind") == "finished" and row.get("loop") == loop
    ]
    outcome = str(finished[-1].get("outcome") or "") if finished else ""
    result = (
        from_legacy_outcome(outcome, detail=str(finished[-1].get("detail") or ""))
        if finished
        else None
    )
    if result is None:
        fallback = RunOutcome.BLOCKED if code == 3 else RunOutcome.FAILED
        return RunResult(fallback, reason_code="missing-run-event", retryable=code != 3)
    projections = [
        projection
        for projection in current().ledger.projections().values()
        if projection.loop == loop
    ]
    if projections:
        latest = max(projections, key=lambda projection: projection.ts)
        if latest.state in {
            ChangeState.PROPOSED,
            ChangeState.AWAITING_HUMAN,
            ChangeState.AWAITING_CHECKS,
        }:
            return RunResult(
                result.outcome,
                lifecycle=latest.state,
                detail=result.detail,
                pr_number=latest.pr,
                candidate_id=latest.finding_id,
                partial=latest.partial,
                retryable=result.retryable,
            )
    return result
