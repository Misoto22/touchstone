"""Step three: a second session that did not write this.

`AGENTS.md`: an implementation session cannot approve its own release. So this
runs cold, read-only, and returns a validated object rather than prose — a
verdict grepped out of free text is decided by word order, which is right for
most phrasings and guaranteed by none, immediately in front of an unattended
production merge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

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


@dataclass(frozen=True, slots=True)
class ReviewAnswer:
    status: Literal["valid", "inconclusive"]
    verdict: Literal["approve", "reject", "skipped"] = "skipped"
    reason: str = ""


def parse_review(raw: str) -> ReviewAnswer:
    if not raw.strip():
        return ReviewAnswer("inconclusive", reason="the review output is missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return ReviewAnswer(
            "inconclusive", reason=f"the review output is not valid JSON: {exc.msg}"
        )
    if not isinstance(payload, dict) or set(payload) != {"verdict", "reason"}:
        return ReviewAnswer(
            "inconclusive", reason="the review output must contain only verdict and reason"
        )
    verdict = payload.get("verdict")
    reason = payload.get("reason")
    if verdict not in {"approve", "reject"}:
        return ReviewAnswer("inconclusive", reason=f"unrecognized review verdict {verdict!r}")
    if not isinstance(reason, str) or not reason.strip():
        return ReviewAnswer("inconclusive", reason="the review reason must be a non-empty string")
    return ReviewAnswer("valid", verdict=verdict, reason=reason)


def run(state: dict[str, Any]) -> dict[str, Any]:
    context = current()
    loop = context.loop(state["loop"])
    worktree = state["worktree"]
    base = f"origin/{context.config.forge.default_branch}"

    from string import Template

    brief = Template(loop.review_prompt()).safe_substitute(dict(loop.context))

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
            "verdict": "skipped",
            "verdict_reason": f"the {context.engine.name} review session failed",
            "outcome": "inconclusive",
            "cost": [session.cost],
        }

    answer = parse_review(session.text)
    if answer.status == "inconclusive":
        return {
            "verdict": "skipped",
            "verdict_reason": answer.reason,
            "outcome": "inconclusive",
            "cost": [session.cost],
        }

    return {
        "verdict": answer.verdict,
        "verdict_reason": answer.reason,
        "cost": [session.cost],
    }
