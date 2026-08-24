"""Step one: look, and say what you found."""

from __future__ import annotations

import json
from typing import Any

from touchstone.nodes.context import current

#: The session writes here rather than to stdout, so a chatty model cannot
#: corrupt the contract.
FINDING_FILE = ".audit-finding.json"


def _clean(context, state: dict[str, Any], detail: str, **extra: Any) -> dict[str, Any]:
    """A run that found nothing, written down.

    Recorded rather than passed over in silence, because the whole point of a
    daily loop is that its absence is noticeable. R-HAR-7 puts it plainly:
    silence in the report means the review did not run, and that is itself a
    finding. A `clean` outcome that leaves no row makes "ran and found nothing"
    indistinguishable from "never started" — which is the one distinction the
    ledger exists to preserve.
    """
    context.ledger.record(
        status="clean",
        risk=None,
        pr=None,
        title=state.get("finding", {}).get("title", ""),
        detail=detail,
    )
    return {"finding": {"status": "none"}, "outcome": "clean", **extra}


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

    finding_path = f"{worktree}/{FINDING_FILE}"
    raw = context.executor.read_text(finding_path)
    # Read once, then gone. It is the loop's own scratch rather than part of the
    # fix, and leaving it for `git add -A` to find is how three such files ended
    # up in a pull request.
    context.executor.run(["rm", "-f", finding_path], timeout=30)
    if not raw:
        # No finding file is a clean pass, and a clean pass is the normal
        # outcome for most runs. Inventing a defect to have something to
        # report is the failure mode this makes cheap to avoid.
        return _clean(context, state, "no finding file written", cost=[session.cost])

    try:
        finding = json.loads(raw)
    except json.JSONDecodeError:
        return _clean(
            context,
            state,
            "the finding file was not valid JSON",
            notes=["the finding file was not valid JSON"],
            cost=[session.cost],
        )

    if finding.get("status") != "proposed":
        return _clean(context, state, finding.get("summary", "nothing found"), cost=[session.cost])

    return {"finding": finding, "cost": [session.cost]}
