"""Step one: look, and say what you found."""

from __future__ import annotations

import json
from typing import Any

from touchstone.nodes.context import current

#: The session writes here rather than to stdout, so a chatty model cannot
#: corrupt the contract.
FINDING_FILE = ".audit-finding.json"


def run(state: dict[str, Any]) -> dict[str, Any]:
    context = current()
    loop = context.loop(state["loop"])
    worktree = state["worktree"]

    brief = loop.prompt()
    handled = context.ledger.handled_titles()
    if handled:
        brief += "\n\n## Already handled — do not raise any of these again\n\n"
        brief += "\n".join(f"- {title}" for title in handled)

    session = context.engine.author(brief, worktree=worktree, denied=loop.protected_paths)
    if not session.ok:
        return {
            "outcome": "held",
            "notes": [f"the {context.engine.name} session failed: {session.detail}"],
            "cost": [session.cost],
        }

    raw = context.executor.read_text(f"{worktree}/{FINDING_FILE}")
    if not raw:
        # No finding file is a clean pass, and a clean pass is the normal
        # outcome for most runs. Inventing a defect to have something to
        # report is the failure mode this makes cheap to avoid.
        return {"finding": {"status": "none"}, "outcome": "clean", "cost": [session.cost]}

    try:
        finding = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "finding": {"status": "none"},
            "outcome": "clean",
            "notes": ["the finding file was not valid JSON"],
            "cost": [session.cost],
        }

    return {"finding": finding, "cost": [session.cost]}
