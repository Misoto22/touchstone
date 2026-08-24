"""GitHub, through `gh`.

Every merge goes through `gh pr merge --auto`, never a poll-then-merge loop.
For the first seconds after a push GitHub reports zero checks and a CLEAN
state, so a script that merges on "green" merges untested code — and a monitor
written that way reported success on a pull request whose checks had not even
registered.
"""

from __future__ import annotations

import json
from typing import Any

from touchstone.execution import Executor


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

    def open_pulls(self, label: str, *, include_drafts: bool) -> list[dict[str, Any]]:
        """Pull requests holding the slot.

        `include_drafts` differs by loop and the difference is not an
        inconsistency. For the code audit only a pull request actually trying
        to merge counts, or the first medium-risk finding would be the last
        thing the loop ever did. For the harness review R-HAR-1 says "never
        more than one open at a time" — open, not open-and-not-a-draft.
        """
        payload = (
            self._json(
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
            or []
        )
        if include_drafts:
            return payload
        return [pull for pull in payload if not pull.get("isDraft")]

    def repository_info(self) -> dict[str, Any] | None:
        payload = self._json(
            ["repo", "view", "--json", "nameWithOwner,defaultBranchRef,autoMergeAllowed"]
        )
        return payload if isinstance(payload, dict) else None

    def labels(self) -> set[str]:
        payload = self._json(["label", "list", "--limit", "100", "--json", "name"]) or []
        return {
            str(item["name"])
            for item in payload
            if isinstance(item, dict) and item.get("name")
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
        payload = self._json(argv) or []
        if not payload:
            return "unknown"
        conclusion = payload[0].get("conclusion")
        return conclusion if conclusion else "pending"

    def create_pull(self, *, base: str, head: str, title: str, body: str, label: str) -> int | None:
        ok, out = self._gh(
            [
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
            ],
            timeout=180,
        )
        if not ok:
            return None
        digits = "".join(char for char in out.rsplit("/", 1)[-1] if char.isdigit())
        return int(digits) if digits else None

    def arm_auto_merge(self, number: int) -> bool:
        ok, _ = self._gh(["pr", "merge", str(number), "--auto", "--squash", "--delete-branch"])
        return ok

    def to_draft(self, number: int) -> bool:
        ok, _ = self._gh(["pr", "ready", str(number), "--undo"])
        return ok

    def add_label(self, number: int, label: str) -> bool:
        ok, _ = self._gh(["pr", "edit", str(number), "--add-label", label])
        return ok

    def close(self, number: int, comment: str) -> bool:
        ok, _ = self._gh(["pr", "close", str(number), "--delete-branch", "--comment", comment])
        return ok
