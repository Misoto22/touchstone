"""Step two: decide who is allowed to ship this.

Risk comes from the finding, and then three checks can only raise it. None of
them can lower it, because every one exists to catch a session that understated
what it was doing — and a check that can be argued down is not a check.
"""

from __future__ import annotations

from typing import Any

from touchstone.nodes.context import current

RISKS = ("low", "medium", "high")


def _changed(context, worktree: str, base: str) -> list[str]:  # type: ignore[no-untyped-def]
    result = context.executor.run(["git", "-C", worktree, "diff", "--name-only", base], timeout=120)
    return [line for line in result.stdout.splitlines() if line.strip()]


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
        return {
            "finding": {"status": "none"},
            "outcome": "clean",
            "notes": ["a finding was claimed but nothing changed on disk"],
        }

    # A run that leaves nothing under the paths it exists to maintain opens no
    # pull request at all. Two merge-triggering loops on one `main` with
    # required checks otherwise invalidate each other's runs every day.
    if loop.require_change_under and not any(
        path.startswith(prefix) for path in paths for prefix in loop.require_change_under
    ):
        return {
            "finding": {"status": "none"},
            "outcome": "clean",
            "notes": ["nothing changed under the paths this loop maintains"],
        }

    for protected in loop.protected_paths:
        hit = [path for path in paths if path.startswith(protected.rstrip("/"))]
        if hit:
            return {
                "risk": "high",
                "escalation": f"touches a protected path ({protected}): {', '.join(hit)}",
            }

    if loop.confine_to:
        stray = [p for p in paths if not any(p.startswith(prefix) for prefix in loop.confine_to)]
        if stray:
            return {"risk": "high", "escalation": f"wrote outside its remit: {', '.join(stray)}"}

    stranded = _twinless(paths, context, worktree, base)
    if stranded:
        return {
            "risk": "high",
            "escalation": f"a translation was left behind: {', '.join(stranded)}",
        }

    return {"risk": risk}
