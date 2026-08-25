"""Step two: decide who is allowed to ship this.

Risk comes from the finding, and then three checks can only raise it. None of
them can lower it, because every one exists to catch a session that understated
what it was doing — and a check that can be argued down is not a check.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from touchstone.nodes.audit import _clean
from touchstone.nodes.context import current

RISKS = ("low", "medium", "high")
BUILTIN_PROTECTED_PATHS = (
    ".github/",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.*",
    "**/credentials",
    "**/credentials.*",
    "*.key",
    "**/*.key",
    "*.pem",
    "**/*.pem",
    "migrations/",
    "**/migrations/",
    "schema/",
    "**/schema/",
    "schema.*",
    "**/schema.*",
    "touchstone.toml",
    "AGENTS.md",
)


def _changed(context, worktree: str, base: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Every path the commit will pick up, tracked or not.

    `git diff --name-only` was the obvious choice and it is the wrong one: it
    lists tracked changes, while `git add -A` sweeps up untracked files too. The
    checks therefore ran against a smaller set than the commit did, and a run
    confined to one directory published three of its own scratch files from the
    repository root without either the confinement or the protected paths
    noticing. A check that sees less than the action it guards is not a guard.
    """
    result = context.executor.run(
        ["git", "-C", worktree, "status", "--porcelain", "--untracked-files=all"], timeout=120
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # `XY path`, and for a rename `XY old -> new`. What the commit carries
        # is the destination.
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def _twinless(paths: list[str], context, worktree: str, base: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Translated documents that moved without their pair.

    One half of a translated pair moving is the same defect whether the file is
    a ledger or a health page; the ledger merely had a test for it, so that one
    was loud and this one was silent for as long as nobody looked.
    """
    stranded: list[str] = []
    for path in paths:
        if not path.endswith(".md"):
            continue
        twin = (
            path[: -len(".zh.md")] + ".md"
            if path.endswith(".zh.md")
            else path[: -len(".md")] + ".zh.md"
        )
        if not context.executor.exists(f"{worktree}/{twin}"):
            continue
        if twin not in paths:
            stranded.append(f"{path} (missing {twin})")
    return stranded


def _matches_path(path: str, pattern: str) -> bool:
    normalized = pattern.removeprefix("./")
    candidates = (normalized, normalized.removeprefix("**/"))
    for candidate in dict.fromkeys(candidates):
        if any(char in candidate for char in "*?["):
            glob = f"{candidate}*" if candidate.endswith("/") else candidate
            if fnmatch.fnmatchcase(path, glob):
                return True
        elif candidate.endswith("/"):
            if path.startswith(candidate):
                return True
        elif path == candidate:
            return True
    return False


def _under(path: str, prefix: str) -> bool:
    """Is `path` inside the directory `prefix` names?

    A bare `startswith` counted `apps/web/apple.ts` as a change under
    `apps/web/app`. Prefixes reach here from two places — generated source
    paths and hand-written `confine_to` entries — so the separator is
    normalised rather than assumed.
    """

    root = prefix.removeprefix("./").rstrip("/")
    if not root or root == ".":
        return True
    return path == root or path.startswith(f"{root}/")


def _protected_paths(loop: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*BUILTIN_PROTECTED_PATHS, *loop.protected_paths)))


def run(state: dict[str, Any]) -> dict[str, Any]:
    context = current()
    loop = context.loop(state["loop"])
    worktree = state["worktree"]
    base = f"origin/{context.config.forge.default_branch}"

    risk = str(state.get("finding", {}).get("risk", "high"))
    if risk not in RISKS:
        return {"risk": "high", "escalation": f"unrecognised risk {risk!r}"}

    paths = _changed(context, worktree, base)
    if not paths:
        return _clean(context, state, "a finding was claimed but nothing changed on disk")

    # A run that leaves nothing under the paths it exists to maintain opens no
    # pull request at all. Two merge-triggering loops on one `main` with
    # required checks otherwise invalidate each other's runs every day.
    if loop.require_change_under and not any(
        _under(path, prefix) for path in paths for prefix in loop.require_change_under
    ):
        return _clean(context, state, "nothing changed under the paths this loop maintains")

    for protected in _protected_paths(loop):
        hit = [path for path in paths if _matches_path(path, protected)]
        if hit:
            return {
                "risk": "high",
                "escalation": f"touches a protected path ({protected}): {', '.join(hit)}",
            }

    if loop.confine_to:
        stray = [p for p in paths if not any(_under(p, prefix) for prefix in loop.confine_to)]
        if stray:
            return {"risk": "high", "escalation": f"wrote outside its remit: {', '.join(stray)}"}

    stranded = _twinless(paths, context, worktree, base)
    if stranded:
        return {
            "risk": "high",
            "escalation": f"a translation was left behind: {', '.join(stranded)}",
        }

    return {"risk": risk}
