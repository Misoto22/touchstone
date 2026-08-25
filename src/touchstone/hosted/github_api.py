"""Small, secret-safe GitHub CLI adapter for hosted setup and diagnostics."""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class GitHubCLI:
    def __init__(
        self,
        repository: str,
        *,
        run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        open_url: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub repository must be OWNER/REPOSITORY")
        self.repository = repository
        self._run = run
        self._open_url = open_url

    def set_actions_secret(self, name: str, value: bytes, *, organization: bool = False) -> bool:
        """Write one Actions secret, keeping an organization secret repository-scoped."""

        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            raise ValueError("GitHub Actions secret name is invalid")
        owner, repository = self.repository.split("/", 1)
        if organization:
            scope = [
                "--org",
                owner,
                "--visibility",
                "selected",
                "--repos",
                repository,
            ]
        else:
            scope = ["--repo", self.repository]
        completed = self._run(
            ["gh", "secret", "set", name, "--app", "actions", *scope],
            input=value,
            capture_output=True,
            timeout=60,
            check=False,
        )
        return completed.returncode == 0

    def delete_actions_secret(self, name: str, *, organization: bool = False) -> bool:
        """Remove a secret this setup stored, so a rejected App leaves nothing usable."""

        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            raise ValueError("GitHub Actions secret name is invalid")
        owner = self.repository.split("/", 1)[0]
        scope = ["--org", owner] if organization else ["--repo", self.repository]
        completed = self._run(
            ["gh", "secret", "delete", name, "--app", "actions", *scope],
            capture_output=True,
            timeout=60,
            check=False,
        )
        return completed.returncode == 0

    def actions_secret_names(self, *, organization: bool = False) -> set[str]:
        """Name every Actions secret this workflow can read.

        A workflow resolves organization and repository secrets together, so a
        setup that stored the App credentials at the organization still has to
        see the model key an operator added on the repository.
        """

        owner = self.repository.split("/", 1)[0]
        scopes = [["--repo", self.repository]]
        if organization:
            scopes.append(["--org", owner])
        names: set[str] = set()
        for scope in scopes:
            payload = self._json(
                ["gh", "secret", "list", "--app", "actions", *scope, "--json", "name"]
            )
            if not isinstance(payload, list):
                continue
            names.update(
                row["name"]
                for row in payload
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            )
        return names

    def installation(self, app_id: int, private_key: bytes) -> dict[str, Any] | None:
        """Read this repository's installation using the App JWT required by GitHub."""

        if app_id <= 0 or not private_key:
            return None
        try:
            token = _app_jwt(app_id, private_key)
            request = urllib.request.Request(
                f"https://api.github.com/repos/{self.repository}/installation",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "touchstone-agent",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with self._open_url(request, timeout=20) as response:
                payload = json.load(response)
        except (OSError, ValueError, TypeError, urllib.error.HTTPError, urllib.error.URLError):
            return None
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


def _app_jwt(app_id: int, private_key: bytes) -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    header = _base64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")))
    claims = _base64url(
        json.dumps(
            {"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
            separators=(",", ":"),
        )
    )
    signing_input = f"{header}.{claims}".encode()
    key = serialization.load_pem_private_key(private_key, password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_base64url_bytes(signature)}"


def _base64url(value: str) -> str:
    return _base64url_bytes(value.encode())


def _base64url_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


__all__ = ["GitHubCLI"]
