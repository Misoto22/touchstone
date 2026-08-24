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


def _session_failure(engine_name: str) -> str:
    """Return a persistable failure note without model output or repository data."""
    return f"the {engine_name} session failed"


def _clean(context: Any, state: dict[str, Any], detail: str, **extra: Any) -> dict[str, Any]:
    """A run that found nothing, written down.

    The runner's event log records every finished run. Keep a clean ledger row
    as well so existing operators can distinguish "ran and found nothing" from
    "never started" while lifecycle projections remain finding-only.
    """
    context.ledger.record(
        status="clean",
        loop=str(state.get("loop") or ""),
        risk=None,
        pr=None,
        title=state.get("finding", {}).get("title", ""),
        detail=detail,
    )
    return {"finding": {"status": "none"}, "outcome": "clean", **extra}


#: Above this an evidence section is refused rather than cut. A census is read
#: to decide whether a ratchet regressed, and half a census cannot answer that
#: while looking like it did — the same reason the review refuses an oversized
#: diff instead of truncating it.
EVIDENCE_LIMIT = 40_000


def _evidence(context: Any, loop: Any, worktree: str) -> str:
    """The sections the brief says were "collected before you".

    Without this the promise is unkept and the session says so: kioku's nightly
    harness review reported the census and the latest CI run unavailable, and
    recorded itself inconclusive under R-HAR-6 — correctly, and every night,
    because nothing ever appended them. A review that cannot verify a ratchet
    is the one thing that review exists to do.

    A command that fails produces a section saying it is unavailable, never a
    missing section. The brief distinguishes the two: an absent heading reads
    as evidence nobody asked for, and an unavailable one as evidence that was
    asked for and could not be had. Only the second is true here.
    """
    if not loop.evidence:
        return ""
    parts = ["\n\n## Evidence collected for you\n"]
    for heading, argv in loop.evidence:
        result = context.executor.run(list(argv), cwd=worktree, timeout=600)
        if not result.ok:
            body = f"unavailable: `{' '.join(argv)}` exited {result.code}"
        elif len(result.stdout) > EVIDENCE_LIMIT:
            body = (
                f"unavailable: `{' '.join(argv)}` produced {len(result.stdout)} characters, "
                f"over the {EVIDENCE_LIMIT} this prompt carries. A partial answer here "
                "would look like a whole one."
            )
        else:
            body = f"```\n{result.stdout.strip()}\n```"
        parts.append(f"\n### {heading}\n\n{body}\n")
    return "".join(parts)


def run(state: dict[str, Any]) -> dict[str, Any]:
    context = current()
    loop = context.loop(state["loop"])
    worktree = state["worktree"]

    brief = loop.prompt()
    handled = context.ledger.handled_titles()
    if handled:
        brief += "\n\n## Already handled — do not raise any of these again\n\n"
        brief += "\n".join(f"- {title}" for title in handled)
    brief += _evidence(context, loop, worktree)

    session = context.engine.author(brief, worktree=worktree, denied=loop.protected_paths)
    if session.blocked:
        # Not clean. The engine was present and thinking and could not act, and
        # recording that as "found nothing" is how six hours of real work and
        # 122k tokens per run disappeared into a ledger that said all was well.
        context.ledger.record(
            status="held",
            risk=None,
            pr=None,
            title="",
            detail=f"the {context.engine.name} session could not act: {session.blocked}",
        )
        return {
            "outcome": "held",
            "notes": [f"blocked: {session.blocked}", f"transcript in {context.config.state_dir}"],
            "cost": [session.cost],
        }

    if not session.ok:
        return {
            "outcome": "held",
            # Engine output may contain model transcript or repository data;
            # graph notes are persisted to the structured event log.
            "notes": [_session_failure(context.engine.name)],
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
        return _clean(
            context,
            state,
            finding.summary,
            finding=finding.to_state(),
            cost=[session.cost],
        )
    return {"finding": finding.to_state(), "cost": [session.cost]}
