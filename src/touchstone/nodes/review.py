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


@dataclass(frozen=True, slots=True)
class _Reviewable:
    """The diff to review, or why no reviewable one could be produced."""

    diff: str = ""
    refusal: str = ""


def _reviewable_diff(context, worktree: str, base: str) -> _Reviewable:  # type: ignore[no-untyped-def]
    """The change exactly as the commit will make it, whole or not at all.

    `git diff <base>` lists tracked changes only, and the commit is `git add -A`,
    which sweeps untracked files as well. So a change that adds a module and its
    tests arrived here as a change importing a module it never defines and
    proving nothing. Two were rejected for "including neither the implementation
    nor the claimed test" while the diff contained both; the reviewer judged
    exactly what it was shown.

    A wrong reject is the survivable direction and not the reason this matters.
    The same blindness approves a change whose entire risk sits in a file the
    reviewer never saw — one step in front of an unattended production merge.

    `--intent-to-add` records paths without their content, which is what makes
    them appear in `git diff` at all. It stages nothing the publishing
    `git add -A` would not stage, and skips ignored files on the same rules.

    `DIFF_LIMIT` is then a refusal, not a truncation, and that is the half of
    this function that was learned the hard way. Slicing was survivable while
    new files were invisible: whatever was cut was tracked work the reviewer had
    at least partly seen. Once new files are included, a single large generated
    one sorts ahead of `z.py` and pushes a security-sensitive edit past the cut
    — a diff that reads complete, and a verdict returned on a change nobody
    read. Reviewing a subset and answering for the whole is the defect this
    function exists to remove; a size limit is no excuse to reintroduce it.

    Parking a change too big to review whole is the correct cost. The loop
    fixes one finding at a time, so a diff of this size is already outside what
    an unattended merge should carry.
    """
    staged = context.executor.run(
        ["git", "-C", worktree, "add", "--all", "--intent-to-add"], timeout=120
    )
    if not staged.ok:
        return _Reviewable(refusal="the change could not be staged for review in full")
    diff = context.executor.run(["git", "-C", worktree, "diff", base], timeout=180).stdout
    if len(diff) > DIFF_LIMIT:
        return _Reviewable(
            refusal=(
                f"the change is {len(diff)} characters, over the {DIFF_LIMIT} a single "
                "review can carry; it needs a person, or splitting"
            )
        )
    return _Reviewable(diff=diff)


def run(state: dict[str, Any]) -> dict[str, Any]:
    context = current()
    loop = context.loop(state["loop"])
    worktree = state["worktree"]
    base = f"origin/{context.config.forge.default_branch}"

    from string import Template

    brief = Template(loop.review_prompt()).safe_substitute(dict(loop.context))

    reviewable = _reviewable_diff(context, worktree, base)
    if reviewable.refusal:
        return {"verdict": "skipped", "verdict_reason": reviewable.refusal}
    finding = state.get("finding", {})
    prompt = (
        f"{brief}\n\n## The change under review\n\n### Stated intent\n\n"
        f"{finding.get('title', '')} — {finding.get('summary', '')}\n\n"
        f"### Diff\n\n```diff\n{reviewable.diff}\n```"
    )

    engine = context.engine_for(loop.name)
    session = engine.review(prompt, worktree=worktree, schema=SCHEMA, model=loop.model)
    if not session.ok:
        return {
            "verdict": "skipped",
            "verdict_reason": f"the {engine.name} review session failed",
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
