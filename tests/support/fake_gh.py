from __future__ import annotations

import json
from typing import Any

from touchstone.execution.base import Result


class FakeGhExecutor:
    where = "local"

    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        self.failures: dict[str, str] = {}
        self.repository = {
            "full_name": "acme/widgets",
            "default_branch": "trunk",
            "allow_auto_merge": True,
        }

    def add_pull(
        self,
        *,
        number: int,
        head_sha: str,
        draft: bool,
        checks: list[str],
        branch: str = "touchstone/run-1",
    ) -> None:
        self.pulls[number] = {
            "number": number,
            "headRefOid": head_sha,
            "headRefName": branch,
            "isDraft": draft,
            "state": "OPEN",
            "mergedAt": None,
            "createdAt": "2026-08-24T00:00:00Z",
            "url": f"https://github.com/acme/widgets/pull/{number}",
            "statusCheckRollup": [{"conclusion": item, "status": "COMPLETED"} for item in checks],
        }

    def fail_next(self, command: str, *, stderr: str) -> None:
        self.failures[command] = stderr

    def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        if argv[0] != "gh":
            return Result(1, "", f"unexpected command: {argv}")
        command = " ".join(argv[1:3])
        failure = self.failures.pop(command, None)
        if failure is not None:
            return Result(1, "", failure)
        if argv[1:3] == ["pr", "view"]:
            number = int(argv[3])
            pull = self.pulls.get(number)
            return Result(0, json.dumps(pull), "") if pull else Result(1, "", "not found")
        if argv[1:3] == ["pr", "list"]:
            branch = argv[argv.index("--head") + 1]
            matches = [pull for pull in self.pulls.values() if pull["headRefName"] == branch]
            return Result(0, json.dumps(matches[:1]), "")
        if argv[1:3] == ["pr", "merge"]:
            return Result(0, "queued", "")
        if argv[1:3] == ["api", "repos/acme/widgets"]:
            return Result(0, json.dumps(self.repository), "")
        if argv[1:3] == ["api", "repos/acme/widgets/branches/trunk"]:
            return Result(0, json.dumps({"protected": True}), "")
        return Result(1, "", f"unsupported gh command: {argv}")

    def read_text(self, path: str) -> str | None:
        return None

    def write_text(self, path: str, text: str) -> None:
        raise AssertionError("unexpected write")

    def exists(self, path: str) -> bool:
        return False
