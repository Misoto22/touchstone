"""Step three: a second session that did not write this.

`AGENTS.md`: an implementation session cannot approve its own release. So this
runs cold, read-only, and returns a validated object rather than prose — a
verdict grepped out of free text is decided by word order, which is right for
most phrasings and guaranteed by none, immediately in front of an unattended
production merge.
"""

from __future__ import annotations

import json
from typing import Any

from touchstone.nodes.context import current

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"enum": ["approve", "reject"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

#: Truncated in Python, never with `| head -c`. `head` closes the pipe once it
#: has its bytes, git takes SIGPIPE, and under `pipefail` the whole run dies —
#: silently, twenty-two minutes and one correct finding in.
DIFF_LIMIT = 60_000


def run(state: dict[str, Any]) -> dict[str, Any]:
    context = current()
    loop = context.loop(state["loop"])
    worktree = state["worktree"]
    base = f"origin/{context.config.forge.default_branch}"

    from string import Template

    brief = Template((loop.brief.parent / "review.md").read_text(encoding="utf-8")).safe_substitute(
        dict(loop.context)
    )

    diff = context.executor.run(["git", "-C", worktree, "diff", base], timeout=180).stdout
    finding = state.get("finding", {})
    prompt = (
        f"{brief}\n\n## The change under review\n\n### Stated intent\n\n"
        f"{finding.get('title', '')} — {finding.get('summary', '')}\n\n"
        f"### Diff\n\n```diff\n{diff[:DIFF_LIMIT]}\n```"
    )

    session = context.engine.review(prompt, worktree=worktree, schema=SCHEMA)
    if not session.ok:
        return {
            "verdict": "reject",
            "verdict_reason": f"the {context.engine.name} review session failed",
            "cost": [session.cost],
        }

    try:
        answer = json.loads(session.text)
        verdict = answer["verdict"]
        reason = str(answer.get("reason", ""))
    except (json.JSONDecodeError, KeyError, TypeError):
        # A verdict that will not parse is a reject. Refusing to read a
        # malformed answer fails in the safe direction: a wrong approve is an
        # incident, a wrong reject is one parked draft.
        return {
            "verdict": "reject",
            "verdict_reason": f"unparseable verdict: {session.text[-300:]}",
            "cost": [session.cost],
        }

    return {"verdict": verdict, "verdict_reason": reason, "cost": [session.cost]}
