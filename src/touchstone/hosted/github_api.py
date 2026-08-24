"""Small, secret-safe GitHub CLI adapter for hosted setup and diagnostics."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any


class GitHubCLI:
    def __init__(
        self,
        repository: str,
        *,
        run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub repository must be OWNER/REPOSITORY")
        self.repository = repository
        self._run = run

    def set_actions_secret(self, name: str, value: bytes) -> bool:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            raise ValueError("GitHub Actions secret name is invalid")
        completed = self._run(
            [
                "gh",
                "secret",
                "set",
                name,
                "--app",
                "actions",
                "--repo",
                self.repository,
            ],
            input=value,
            capture_output=True,
            timeout=60,
            check=False,
        )
        return completed.returncode == 0

    def actions_secret_names(self) -> set[str]:
        payload = self._json(
            [
                "gh",
                "secret",
                "list",
                "--app",
                "actions",
                "--repo",
                self.repository,
                "--json",
                "name",
            ]
        )
        if not isinstance(payload, list):
            return set()
        return {
            row["name"]
            for row in payload
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }

    def installation(self) -> dict[str, Any] | None:
        payload = self._json(
            ["gh", "api", f"repos/{self.repository}/installation"],
        )
        return payload if isinstance(payload, dict) else None

    def workflow(self, name: str = "touchstone.yml") -> dict[str, Any] | None:
        payload = self._json(
            ["gh", "api", f"repos/{self.repository}/actions/workflows/{name}"],
        )
        return payload if isinstance(payload, dict) else None

    def repository_info(self) -> dict[str, Any] | None:
        payload = self._json(["gh", "api", f"repos/{self.repository}"])
        return payload if isinstance(payload, dict) else None

    def environment(self, name: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name):
            raise ValueError("GitHub Environment name is invalid")
        payload = self._json(["gh", "api", f"repos/{self.repository}/environments/{name}"])
        return payload if isinstance(payload, dict) else None

    def _json(self, argv: list[str]) -> Any:
        completed = self._run(
            argv,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            return None
        raw = completed.stdout
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


__all__ = ["GitHubCLI"]
