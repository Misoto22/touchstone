"""GitHub, through `gh`.

Every merge goes through `gh pr merge --auto`, never a poll-then-merge loop.
For the first seconds after a push GitHub reports zero checks and a CLEAN
state, so a script that merges on "green" merges untested code — and a monitor
written that way reported success on a pull request whose checks had not even
registered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from touchstone.execution import Executor


class ForgeUnavailable(RuntimeError):
    """GitHub state could not be distinguished from an empty result."""


@dataclass(frozen=True, slots=True)
class OperationResult:
    ok: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True, slots=True)
class PullState:
    number: int
    head_sha: str
    branch: str
    draft: bool
    check_state: str
    merged_at: str | None
    closed: bool
    created_at: str
    url: str


class Forge:
    def __init__(self, slug: str, executor: Executor) -> None:
        self._slug = slug
        self._exec = executor

    def _gh(self, argv: list[str], *, timeout: int = 120) -> tuple[bool, str]:
        result = self._exec.run(["gh", *argv, "--repo", self._slug], timeout=timeout)
        return (result.ok, result.stdout.strip() or result.stderr.strip())

    def _json(self, argv: list[str]) -> Any:
        ok, out = self._gh(argv)
        if not ok:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    def _api_json(self, endpoint: str) -> Any:
        result = self._exec.run(["gh", "api", endpoint], timeout=120)
        if not result.ok:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def open_pulls(self, label: str, *, include_drafts: bool) -> list[dict[str, Any]] | None:
        """Pull requests holding the slot.

        `include_drafts` differs by loop and the difference is not an
        inconsistency. For the code audit only a pull request actually trying
        to merge counts, or the first medium-risk finding would be the last
        thing the loop ever did. For the harness review R-HAR-1 says "never
        more than one open at a time" — open, not open-and-not-a-draft.
        """
        payload = self._json(
            [
                "pr",
                "list",
                "--label",
                label,
                "--state",
                "open",
                "--json",
                "number,isDraft,createdAt,url",
            ]
        )
        if not isinstance(payload, list):
            return None
        for pull in payload:
            number = pull.get("number") if isinstance(pull, dict) else None
            if (
                not isinstance(pull, dict)
                or isinstance(number, bool)
                or not isinstance(number, int)
                or number <= 0
                or not isinstance(pull.get("isDraft"), bool)
            ):
                return None
        pulls = payload
        if include_drafts:
            return pulls
        return [pull for pull in pulls if not pull.get("isDraft")]

    def repository_info(self) -> dict[str, Any] | None:
        payload = self._api_json(f"repos/{self._slug}")
        if not isinstance(payload, dict):
            return None
        name = payload.get("full_name")
        branch = payload.get("default_branch")
        auto_merge = payload.get("allow_auto_merge")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(branch, str)
            or not branch
            or not isinstance(auto_merge, bool)
        ):
            return None
        return {
            "nameWithOwner": name,
            "defaultBranchRef": {"name": branch},
            "autoMergeAllowed": auto_merge,
        }

    def branch_protection(self, branch: str) -> bool | None:
        payload = self._api_json(f"repos/{self._slug}/branches/{quote(branch, safe='')}")
        if not isinstance(payload, dict) or not isinstance(payload.get("protected"), bool):
            return None
        return payload["protected"]

    def labels(self) -> set[str]:
        payload = self._json(["label", "list", "--limit", "100", "--json", "name"])
        if not isinstance(payload, list):
            return set()
        return {
            item["name"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
        }

    def ensure_label(self, name: str, *, color: str, description: str) -> bool:
        ok, _ = self._gh(
            [
                "label",
                "create",
                name,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ]
        )
        return ok

    def latest_run(self, workflow: str, *, branch: str | None = None) -> str:
        """A workflow's most recent conclusion, or `pending` when it has none.

        `//` in jq only substitutes null, and an in-progress run reports an
        empty string — which read as neither success nor failure and made the
        log a question rather than an answer.
        """
        argv = ["run", "list", "--workflow", workflow, "--limit", "1", "--json", "conclusion"]
        if branch:
            argv += ["--branch", branch]
        payload = self._json(argv)
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return "unknown"
        conclusion = payload[0].get("conclusion")
        return conclusion if isinstance(conclusion, str) and conclusion else "pending"

    def create_pull(
        self,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        label: str,
        draft: bool = False,
    ) -> int | None:
        argv = [
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
            "--label",
            label,
        ]
        if draft:
            argv.append("--draft")
        ok, out = self._gh(argv, timeout=180)
        if not ok:
            return None
        digits = "".join(char for char in out.rsplit("/", 1)[-1] if char.isdigit())
        return int(digits) if digits else None

    def pull(self, number: int) -> PullState | None:
        payload = self._json(
            [
                "pr",
                "view",
                str(number),
                "--json",
                "number,headRefOid,headRefName,isDraft,state,mergedAt,createdAt,url,statusCheckRollup",
            ]
        )
        return _pull_state(payload) if isinstance(payload, dict) else None

    def pull_for_branch(self, branch: str) -> PullState | None:
        payload = self._json(
            [
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--limit",
                "1",
                "--json",
                "number,headRefOid,headRefName,isDraft,state,mergedAt,createdAt,url,statusCheckRollup",
            ]
        )
        if not isinstance(payload, list):
            raise ForgeUnavailable("could not verify the existing pull request")
        if not payload:
            return None
        if len(payload) != 1 or not isinstance(payload[0], dict):
            raise ForgeUnavailable("existing pull request response is malformed")
        pull = _pull_state(payload[0])
        if pull is None:
            raise ForgeUnavailable("existing pull request response is malformed")
        return pull

    def arm_auto_merge(self, number: int) -> OperationResult:
        ok, detail = self._gh(["pr", "merge", str(number), "--auto", "--squash", "--delete-branch"])
        return OperationResult(ok, "" if ok else detail)

    def to_draft(self, number: int) -> OperationResult:
        ok, detail = self._gh(["pr", "ready", str(number), "--undo"])
        return OperationResult(ok, "" if ok else detail)

    def mark_ready(self, number: int) -> OperationResult:
        ok, detail = self._gh(["pr", "ready", str(number)])
        return OperationResult(ok, "" if ok else detail)

    def add_label(self, number: int, label: str) -> OperationResult:
        ok, detail = self._gh(["pr", "edit", str(number), "--add-label", label])
        return OperationResult(ok, "" if ok else detail)

    def close(self, number: int, comment: str) -> OperationResult:
        ok, detail = self._gh(["pr", "close", str(number), "--delete-branch", "--comment", comment])
        return OperationResult(ok, "" if ok else detail)


def _pull_state(payload: dict[str, Any]) -> PullState | None:
    number = payload.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    for key in ("headRefOid", "headRefName", "state", "createdAt", "url"):
        if key in payload and not isinstance(payload[key], str):
            return None
    if "isDraft" in payload and not isinstance(payload["isDraft"], bool):
        return None
    if (
        "mergedAt" in payload
        and payload["mergedAt"] is not None
        and not isinstance(payload["mergedAt"], str)
    ):
        return None
    merged_at = payload.get("mergedAt")
    state = str(payload.get("state") or "").upper()
    return PullState(
        number=number,
        head_sha=str(payload.get("headRefOid") or ""),
        branch=str(payload.get("headRefName") or ""),
        draft=bool(payload.get("isDraft")),
        check_state=_check_state(payload.get("statusCheckRollup")),
        merged_at=str(merged_at) if merged_at else None,
        closed=state == "CLOSED" and not merged_at,
        created_at=str(payload.get("createdAt") or ""),
        url=str(payload.get("url") or ""),
    )


def _check_state(raw: Any) -> str:
    if not isinstance(raw, list) or not raw:
        return "unknown"
    conclusions: list[str] = []
    pending = False
    for item in raw:
        if not isinstance(item, dict):
            pending = True
            continue
        conclusion = str(item.get("conclusion") or "").upper()
        status = str(item.get("status") or "").upper()
        if conclusion:
            conclusions.append(conclusion)
        if not conclusion or status not in {"", "COMPLETED"}:
            pending = True
    if any(
        conclusion
        in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
        for conclusion in conclusions
    ):
        return "failure"
    if pending:
        return "pending"
    if conclusions and all(item in {"SUCCESS", "NEUTRAL", "SKIPPED"} for item in conclusions):
        return "success"
    return "unknown"


__all__ = ["Forge", "ForgeUnavailable", "OperationResult", "PullState"]
