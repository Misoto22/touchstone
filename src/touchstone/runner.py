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
import time
import uuid
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from touchstone.config import Config
from touchstone.events import EventLog, run_event
from touchstone.graph import build
from touchstone.lifecycle import RepositoryLifecycle
from touchstone.nodes.context import current


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
    if held:
        raise Held(f"slot held by #{held[0]['number']}: {held[0].get('url', '')}")

    _health_gate(config)


def _health_gate(config: Config) -> None:
    """Require explicit success from every project-configured workflow."""
    forge = current().forge
    unhealthy: list[str] = []
    for workflow in config.forge.required_workflows:
        conclusion = forge.latest_run(workflow, branch=config.forge.default_branch)
        if conclusion != "success":
            unhealthy.append(f"{workflow}={conclusion}")
    if unhealthy:
        raise Held(f"production not known good: {' '.join(unhealthy)}")


def _worktree(config: Config) -> tuple[str, str]:
    context = current()
    branch = f"audit/{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}"
    path = str(Path(config.state_dir) / "worktree")
    repo = str(config.repo_path)
    base = f"origin/{config.forge.default_branch}"

    context.executor.run(["git", "-C", repo, "fetch", "--prune", "--quiet", "origin"], timeout=300)
    context.executor.run(["git", "-C", repo, "worktree", "prune"], timeout=60)
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


def _teardown(config: Config, path: str, branch: str, *, published: bool) -> None:
    context = current()
    repo = str(config.repo_path)
    context.executor.run(["git", "-C", repo, "worktree", "remove", "--force", path], timeout=60)
    context.executor.run(["git", "-C", repo, "worktree", "prune"], timeout=60)
    if not published:
        # Removing a worktree leaves its branch. A run that published wants it;
        # a clean pass or a crash leaves a dead ref nothing collects, and at one
        # run an hour that is a branch list nobody can read within a week.
        context.executor.run(["git", "-C", repo, "branch", "-D", branch], timeout=60)


def execute(config: Config, *, loop: str, dry_run: bool = False) -> int:
    state_dir = Path(config.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    context = current()
    event_log = EventLog(state_dir / "events.jsonl")
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    event_log.append(run_event(config, run_id=run_id, kind="started", loop=loop))
    final_outcome = "held"
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
                outcome="held",
                duration_seconds=time.monotonic() - started,
                detail=str(held),
            )
        )
        return 0

    path = branch = ""
    published = False
    try:
        RepositoryLifecycle(
            context.forge,
            context.ledger,
            reap_after_hours=config.forge.reap_after_hours,
        ).reconcile(config.loop(loop), dt.datetime.now(dt.UTC))
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
        outcome = final.get("outcome") or ("escalated" if paused else "clean")
        final_outcome = outcome
        final_pr = int(final["pr"]) if final.get("pr") is not None else None
        final_risk = str(final.get("risk") or "")
        final_verdict = str(final.get("verdict") or "")
        reported_costs = [value for value in final.get("cost", []) if value is not None]
        final_cost = sum(reported_costs) if reported_costs else None
        final_detail = "; ".join(str(note) for note in final.get("notes", []))
        published = outcome in {"merging", "escalated"}
        print(f"{loop}: {outcome}" + (f" #{final['pr']}" if final.get("pr") else ""))
        for note in final.get("notes", []):
            print(f"  {note}")
        if paused:
            print(f"  parked; resume with: touchstone resume {thread_id} merge|close")
        return 0
    except Held as held:
        print(held)
        final_detail = str(held)
        context.ledger.record(status="held", title="", detail=str(held))
        return 0
    finally:
        if path:
            _teardown(config, path, branch, published=published)
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

    state_dir = Path(config.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock = _lock(state_dir)
    except Held as held:
        print(held)
        return 0

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

            context = current()
            RepositoryLifecycle(
                context.forge,
                context.ledger,
                reap_after_hours=config.forge.reap_after_hours,
            ).reconcile(config.loop(loop_name), dt.datetime.now(dt.UTC))
            if answer == "merge":
                _health_gate(config)
            final = app.invoke(Command(resume=answer), graph_config)
        print(f"{thread}: {final.get('outcome', 'unknown')}")
        for note in final.get("notes", []):
            print(f"  {note}")
        return 0
    except Held as held:
        print(held)
        return 0
    finally:
        shutil.rmtree(lock, ignore_errors=True)
