"""Resumable, browser-confirmed GitHub App Manifest setup."""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
import secrets
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol

from touchstone.config import ConfigError
from touchstone.hosted.github_api import GitHubCLI

_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "actions": "read",
    "issues": "write",
}
_REQUIRED_SECRETS = {
    "TOUCHSTONE_APP_ID",
    "TOUCHSTONE_APP_PRIVATE_KEY",
    "TOUCHSTONE_STATE_KEY",
}


class SetupGitHub(Protocol):
    def set_actions_secret(self, name: str, value: bytes) -> bool: ...

    def actions_secret_names(self) -> set[str]: ...

    def installation(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class AppManifest:
    name: str
    url: str
    redirect_url: str
    default_permissions: dict[str, str]
    hook_attributes: dict[str, bool]
    public: bool = False
    description: str = "Touchstone pull-request publisher for one repository"
    default_events: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SetupOptions:
    check: bool = False
    owner_type: Literal["user", "organization"] = "user"
    callback_port: int = 8917
    callback_timeout_seconds: int = 300
    manual_code: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.callback_port <= 65535:
            raise ValueError("callback port must be between 1 and 65535")
        if not 30 <= self.callback_timeout_seconds <= 3600:
            raise ValueError("callback timeout must be between 30 and 3600 seconds")


@dataclass(frozen=True, slots=True)
class PartialSetup:
    repository: str
    state: Literal["partial", "complete"]
    step: str
    app_id: int | None = None
    app_slug: str = ""
    updated_at: str = ""
    version: int = 1


@dataclass(frozen=True, slots=True)
class SetupReport:
    state: Literal["partial", "complete"]
    step: str
    repair: str = ""
    app_id: int | None = None
    app_slug: str = ""


def build_manifest(*, owner: str, repository: str, redirect_url: str) -> AppManifest:
    if not owner or not repository:
        raise ValueError("GitHub owner and repository are required")
    parsed = urllib.parse.urlsplit(redirect_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("GitHub App manifest redirect must use loopback HTTP")
    name = f"{owner}-{repository}-touchstone"[:100]
    return AppManifest(
        name=name,
        url="https://github.com/Misoto22/touchstone",
        redirect_url=redirect_url,
        default_permissions=dict(_PERMISSIONS),
        hook_attributes={"active": False},
    )


def parse_callback(query: str, *, expected_state: str) -> str:
    values = urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=True)
    states = values.get("state", [])
    codes = values.get("code", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
        raise ValueError("GitHub App manifest callback state does not match")
    if len(codes) != 1 or not codes[0] or len(codes[0]) > 512:
        raise ValueError("GitHub App manifest callback code is invalid")
    return codes[0]


def exchange_manifest_code(code: str, *, timeout: float = 20.0) -> dict[str, Any]:
    if not code or len(code) > 512:
        raise ValueError("GitHub App manifest code is invalid")
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{urllib.parse.quote(code, safe='')}/conversions",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "touchstone-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
        raise ConfigError(
            "GitHub App manifest conversion failed; the one-time code may be expired or used"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigError("GitHub App manifest conversion returned an invalid response")
    return payload


class ActionsSetup:
    def __init__(
        self,
        config: Any,
        *,
        github: SetupGitHub | None = None,
        code_provider: Callable[[AppManifest, str], str] | None = None,
        exchange: Callable[[str], dict[str, Any]] = exchange_manifest_code,
        open_browser: Callable[[str], Any] = webbrowser.open,
        confirm_installation: Callable[[str], bool] | None = None,
    ) -> None:
        self.config = config
        self.github = github or GitHubCLI(config.forge.slug)
        self._code_provider = code_provider
        self._exchange = exchange
        self._open_browser = open_browser
        self._confirm_installation = confirm_installation or _confirm_installation
        self._options = SetupOptions()

    @property
    def state_path(self) -> Path:
        return Path(self.config.state_dir).expanduser().resolve() / "actions-setup.json"

    def run(self, options: SetupOptions) -> SetupReport:
        self._options = options
        previous = self._read_state()
        if options.check:
            return self._check(previous)
        if previous:
            report = self._check(previous)
            if report.state == "complete":
                return report
            return report

        owner, repository = self.config.forge.slug.split("/", 1)
        redirect = f"http://127.0.0.1:{options.callback_port}/callback"
        manifest = build_manifest(owner=owner, repository=repository, redirect_url=redirect)
        csrf_state = secrets.token_urlsafe(32)
        code = options.manual_code.strip()
        if not code:
            provider = self._code_provider or self._browser_manifest_code
            code = provider(manifest, csrf_state)
        if not code:
            return self._partial("manifest-code-required", repair="rerun setup with --manual-code")
        payload = self._exchange(code)
        app_id, app_slug, pem = _conversion_fields(payload)
        self._write_state(
            PartialSetup(
                self.config.forge.slug, "partial", "app-created", app_id, app_slug
            )
        )

        if not self.github.set_actions_secret("TOUCHSTONE_APP_ID", str(app_id).encode()):
            return self._record_secret_failure(app_id, app_slug, "app-id-secret")
        state_key = bytearray(base64.urlsafe_b64encode(secrets.token_bytes(32)))
        try:
            if not self.github.set_actions_secret("TOUCHSTONE_STATE_KEY", bytes(state_key)):
                return self._record_secret_failure(app_id, app_slug, "state-key-secret")
        finally:
            for index in range(len(state_key)):
                state_key[index] = 0

        private_key = bytearray(pem.encode("utf-8"))
        try:
            if not self.github.set_actions_secret(
                "TOUCHSTONE_APP_PRIVATE_KEY", bytes(private_key)
            ):
                report = self._partial(
                    "private-key-repair-required",
                    app_id=app_id,
                    app_slug=app_slug,
                    repair=(
                        "generate a replacement key in the App settings, pipe it to "
                        "'gh secret set TOUCHSTONE_APP_PRIVATE_KEY --app actions', "
                        "then rerun setup"
                    ),
                )
                self._write_state(
                    PartialSetup(
                        self.config.forge.slug,
                        "partial",
                        report.step,
                        app_id,
                        app_slug,
                    )
                )
                return report
        finally:
            for index in range(len(private_key)):
                private_key[index] = 0

        installation_url = f"https://github.com/apps/{app_slug}/installations/new"
        self._open_browser(installation_url)
        if not self._confirm_installation(installation_url):
            report = self._partial(
                "installation-required",
                app_id=app_id,
                app_slug=app_slug,
                repair=f"install the App for {self.config.forge.slug}, then rerun --check",
            )
            self._write_state(
                PartialSetup(
                    self.config.forge.slug,
                    "partial",
                    report.step,
                    app_id,
                    app_slug,
                )
            )
            return report
        verification = self._installation_report(app_id, app_slug)
        if verification.state != "complete":
            self._write_state(
                PartialSetup(
                    self.config.forge.slug,
                    "partial",
                    verification.step,
                    app_id,
                    app_slug,
                )
            )
            return verification
        self._write_state(
            PartialSetup(self.config.forge.slug, "complete", "configured", app_id, app_slug)
        )
        return verification

    def _check(self, previous: PartialSetup | None) -> SetupReport:
        if previous is None:
            return self._partial(
                "not-configured", repair="run 'touchstone actions setup' interactively"
            )
        if previous.repository != self.config.forge.slug:
            return self._partial("repository-mismatch", repair="remove stale local setup state")
        if previous.app_id is None or not previous.app_slug:
            return self._partial(previous.step, repair="rerun 'touchstone actions setup'")
        secrets_present = self.github.actions_secret_names()
        missing = sorted(_REQUIRED_SECRETS - secrets_present)
        if missing:
            return self._partial(
                "secrets-missing",
                app_id=previous.app_id,
                app_slug=previous.app_slug,
                repair=f"repair Actions secret metadata: {', '.join(missing)}",
            )
        return self._installation_report(previous.app_id, previous.app_slug)

    def _installation_report(self, app_id: int, app_slug: str) -> SetupReport:
        installation = self.github.installation()
        if not isinstance(installation, dict) or installation.get("app_id") != app_id:
            return self._partial(
                "installation-missing",
                app_id=app_id,
                app_slug=app_slug,
                repair=f"install https://github.com/apps/{app_slug} for this repository",
            )
        permissions = installation.get("permissions")
        if not isinstance(permissions, dict) or any(
            permissions.get(name) != access for name, access in _PERMISSIONS.items()
        ):
            return self._partial(
                "permissions-mismatch",
                app_id=app_id,
                app_slug=app_slug,
                repair="update the GitHub App installation to the documented permissions",
            )
        return SetupReport("complete", "configured", app_id=app_id, app_slug=app_slug)

    def _record_secret_failure(self, app_id: int, app_slug: str, step: str) -> SetupReport:
        report = self._partial(
            step,
            app_id=app_id,
            app_slug=app_slug,
            repair="rerun setup to repair missing Actions secrets",
        )
        self._write_state(
            PartialSetup(self.config.forge.slug, "partial", step, app_id, app_slug)
        )
        return report

    def _partial(
        self,
        step: str,
        *,
        repair: str = "",
        app_id: int | None = None,
        app_slug: str = "",
    ) -> SetupReport:
        return SetupReport("partial", step, repair, app_id, app_slug)

    def _read_state(self) -> PartialSetup | None:
        if not self.state_path.is_file():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return PartialSetup(**payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_state(self, state: PartialSetup) -> None:
        target = self.state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        payload["updated_at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _browser_manifest_code(self, manifest: AppManifest, state: str) -> str:
        owner = self.config.forge.slug.split("/", 1)[0]
        if self._options.owner_type == "organization":
            action = f"https://github.com/organizations/{urllib.parse.quote(owner)}/settings/apps/new"
        else:
            action = "https://github.com/settings/apps/new"
        callback = _ManifestCallback(
            manifest,
            state,
            action=action,
            port=self._options.callback_port,
        )
        with callback:
            self._open_browser(callback.start_url)
            return callback.wait(self._options.callback_timeout_seconds)


class _ManifestCallback:
    def __init__(
        self,
        manifest: AppManifest,
        state: str,
        *,
        action: str,
        port: int,
    ) -> None:
        self.manifest = manifest
        self.state = state
        self.action = action
        self.port = port
        self.code = ""
        self.error = ""
        self.ready = threading.Event()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def start_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/start"

    def __enter__(self) -> _ManifestCallback:
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/start":
                    body = _registration_form(callback.action, callback.manifest, callback.state)
                    self._reply(200, body)
                    return
                if parsed.path == "/callback":
                    try:
                        callback.code = parse_callback(
                            parsed.query, expected_state=callback.state
                        )
                        self._reply(200, "Touchstone GitHub App created. Return to the terminal.")
                    except ValueError as exc:
                        callback.error = str(exc)
                        self._reply(400, "Touchstone rejected this callback.")
                    finally:
                        callback.ready.set()
                    return
                self._reply(404, "Not found")

            def _reply(self, status: int, body: str) -> None:
                encoded = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            raise ConfigError(
                f"could not bind loopback callback port {self.port}; use --callback-port"
            ) from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def wait(self, timeout: int) -> str:
        if not self.ready.wait(timeout):
            raise ConfigError(
                "timed out waiting for GitHub App creation; rerun with --manual-code if needed"
            )
        if self.error:
            raise ConfigError(self.error)
        return self.code

    def __exit__(self, *_args: object) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def _registration_form(action: str, manifest: AppManifest, state: str) -> str:
    encoded_manifest = html.escape(manifest.to_json(), quote=True)
    return (
        "<!doctype html><meta charset='utf-8'><title>Touchstone setup</title>"
        f"<form id='setup' method='post' action='{html.escape(action, quote=True)}'>"
        f"<input type='hidden' name='manifest' value='{encoded_manifest}'>"
        f"<input type='hidden' name='state' value='{html.escape(state, quote=True)}'>"
        "<button type='submit'>Review GitHub App</button></form>"
        "<script>document.getElementById('setup').submit()</script>"
    )


def _conversion_fields(payload: dict[str, Any]) -> tuple[int, str, str]:
    app_id = payload.get("id")
    app_slug = payload.get("slug")
    pem = payload.get("pem")
    if (
        isinstance(app_id, bool)
        or not isinstance(app_id, int)
        or app_id <= 0
        or not isinstance(app_slug, str)
        or not app_slug
        or not isinstance(pem, str)
        or "-----BEGIN" not in pem
        or "PRIVATE KEY-----" not in pem
        or len(pem) > 1024 * 1024
    ):
        raise ConfigError("GitHub App manifest conversion omitted required one-time credentials")
    return app_id, app_slug, pem


def _confirm_installation(_url: str) -> bool:
    try:
        answer = input("Install the App for this repository, then type 'yes': ").strip().lower()
    except EOFError:
        return False
    return answer == "yes"


__all__ = [
    "ActionsSetup",
    "AppManifest",
    "PartialSetup",
    "SetupOptions",
    "SetupReport",
    "build_manifest",
    "exchange_manifest_code",
    "parse_callback",
]
