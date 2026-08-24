"""Step one: look, and say what you found."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from touchstone.nodes.context import current

#: The session writes here rather than to stdout, so a chatty model cannot
#: corrupt the contract.
FINDING_FILE = ".audit-finding.json"


@dataclass(frozen=True, slots=True)
class Finding:
    status: Literal["none", "proposed", "inconclusive"]
    summary: str = ""
    risk: str = ""
    title: str = ""
    commit_subject: str = ""
    rationale: str = ""
    detail: str = ""

    def to_state(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value}


def parse_finding(raw: str) -> Finding:
    """Validate the complete author-to-runner contract without guessing."""
    if not raw.strip():
        return Finding("inconclusive", detail="the finding output is missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Finding("inconclusive", detail=f"the finding output is not valid JSON: {exc.msg}")
    if not isinstance(payload, dict):
        return Finding("inconclusive", detail="the finding output must be a JSON object")

    status = payload.get("status")
    if status == "none":
        error = _fields(payload, required={"status", "summary"}, allowed={"status", "summary"})
        if error:
            return Finding("inconclusive", detail=error)
        return Finding("none", summary=str(payload["summary"]))
    if status == "proposed":
        required = {"status", "risk", "title", "commit_subject", "summary", "rationale"}
        error = _fields(payload, required=required, allowed=required)
        if error:
            return Finding("inconclusive", detail=error)
        risk = payload["risk"]
        if risk not in {"low", "medium", "high"}:
            return Finding(
                "inconclusive", detail=f"risk must be low, medium, or high, not {risk!r}"
            )
        if len(str(payload["commit_subject"])) > 72:
            return Finding("inconclusive", detail="commit_subject must be 72 characters or fewer")
        return Finding(
            "proposed",
            risk=str(risk),
            title=str(payload["title"]),
            commit_subject=str(payload["commit_subject"]),
            summary=str(payload["summary"]),
            rationale=str(payload["rationale"]),
        )
    return Finding("inconclusive", detail=f"unrecognized finding status {status!r}")


def _fields(payload: dict[str, Any], *, required: set[str], allowed: set[str]) -> str:
    missing = sorted(required - set(payload))
    if missing:
        return f"the finding output is missing {missing[0]}"
    extra = sorted(set(payload) - allowed)
    if extra:
        return f"the finding output has unknown field {extra[0]}"
    for key in sorted(required - {"status"}):
        if not isinstance(payload[key], str) or not payload[key].strip():
            return f"the finding field {key} must be a non-empty string"
    return ""


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
    finding = parse_finding(raw or "")
    if finding.status == "inconclusive":
        return {
            "finding": finding.to_state(),
            "outcome": "inconclusive",
            "notes": [finding.detail],
            "cost": [session.cost],
        }
    if finding.status == "none":
        return {"finding": finding.to_state(), "outcome": "clean", "cost": [session.cost]}
    return {"finding": finding.to_state(), "cost": [session.cost]}
