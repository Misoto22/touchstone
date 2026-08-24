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
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from harness_loop.config import Config
from harness_loop.graph import build
from harness_loop.nodes.context import current


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

    paused = Path(config.state_dir) / "PAUSED"
    if paused.exists():
        raise Held(f"paused: {paused.read_text().strip()}")

    # R-HAR-1 says one harness review open at a time — open, not
    # open-and-not-a-draft. For the code audit the opposite is right: if drafts
    # held the slot, the first medium-risk finding would be the last thing the
    # loop ever did.
    include_drafts = bool(loop.require_change_under)
    held = context.forge.open_pulls(loop.label, include_drafts=include_drafts)
    if held:
        raise Held(f"slot held by #{held[0]['number']}: {held[0].get('url', '')}")

    if dry_run:
        # A dry run cannot publish, so there is nothing for the health gate to
        # protect — and a held rehearsal makes the engines impossible to
        # compare, since whichever is tried mid-CI simply never runs.
        return

    # Only an explicit success passes. Treating "anything but failure" as
    # healthy admits cancelled, timed-out and still-running checks, and those
    # are not reassurance — they are the absence of an answer.
    verify = context.forge.latest_run("verify-deploy.yml")
    ci = context.forge.latest_run("ci.yml", branch=config.forge.default_branch)
    if verify != "success" or ci != "success":
        raise Held(f"production not known good: verify-deploy={verify} ci={ci}")


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

    try:
        lock = _lock(state_dir)
    except Held as held:
        print(held)
        return 0

    path = branch = ""
    published = False
    try:
        _gates(config, loop, dry_run=dry_run)
        path, branch = _worktree(config)
        with SqliteSaver.from_conn_string(str(state_dir / "checkpoints.sqlite")) as saver:
            app = build().compile(checkpointer=saver)
            thread = {"configurable": {"thread_id": f"{loop}-{branch}"}}
            final = app.invoke({"loop": loop, "worktree": path, "branch": branch}, thread)
        published = final.get("outcome") in {"merging", "escalated"}
        print(
            f"{loop}: {final.get('outcome', 'clean')}"
            + (f" #{final['pr']}" if final.get("pr") else "")
        )
        if final.get("outcome") == "escalated":
            print(f"  parked; resume with: harness-loop resume {loop}-{branch} merge|close")
        context.ledger.record(
            status=final.get("outcome", "clean"),
            risk=final.get("risk"),
            pr=final.get("pr"),
            title=final.get("finding", {}).get("title", ""),
            detail="; ".join(final.get("notes", [])),
        )
        return 0
    except Held as held:
        print(held)
        context.ledger.record(status="held", title="", detail=str(held))
        return 0
    finally:
        if path:
            _teardown(config, path, branch, published=published)
        shutil.rmtree(lock, ignore_errors=True)


def resume(config: Config, *, thread: str, answer: str) -> int:
    """Continue a parked thread from where it stopped.

    The whole reason `park` is an interrupt rather than an exit: a person's
    answer reaches the run that asked, instead of the next hour starting over
    and paying for another audit to reach the same conclusion.
    """
    from langgraph.types import Command

    state_dir = Path(config.state_dir)
    with SqliteSaver.from_conn_string(str(state_dir / "checkpoints.sqlite")) as saver:
        app = build().compile(checkpointer=saver)
        final = app.invoke(Command(resume=answer), {"configurable": {"thread_id": thread}})
    print(f"{thread}: {final.get('outcome', 'unknown')}")
    return 0
