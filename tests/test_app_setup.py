from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.hosted.app_setup import (
    ActionsSetup,
    SetupOptions,
    build_manifest,
    parse_callback,
)


class FakeGitHub:
    def __init__(self, *, fail_secret: str = "") -> None:
        self.fail_secret = fail_secret
        self.secrets: dict[str, bytes] = {}
        self.opened: list[str] = []

    def set_actions_secret(self, name: str, value: bytes) -> bool:
        if name == self.fail_secret:
            return False
        self.secrets[name] = value
        return True

    def actions_secret_names(self) -> set[str]:
        return set(self.secrets)

    def installation(self):  # type: ignore[no-untyped-def]
        return {
            "app_id": 42,
            "repository_selection": "selected",
            "permissions": {
                "actions": "read",
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
            },
        }


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        repo_path=tmp_path,
        state_dir=tmp_path / ".touchstone" / "state",
        forge=SimpleNamespace(slug="acme/widgets"),
    )


def _conversion() -> dict[str, object]:
    return {
        "id": 42,
        "slug": "acme-touchstone",
        "client_id": "Iv1.client",
        "pem": "-----BEGIN RSA PRIVATE KEY-----\nprivate\n-----END RSA PRIVATE KEY-----\n",
        "html_url": "https://github.com/apps/acme-touchstone",
    }


def test_manifest_uses_least_privilege_repository_permissions() -> None:
    manifest = build_manifest(
        owner="acme",
        repository="widgets",
        redirect_url="http://127.0.0.1:8917/callback",
    )

    assert manifest.default_permissions == {
        "contents": "write",
        "pull_requests": "write",
        "actions": "read",
        "issues": "write",
    }
    assert manifest.public is False
    assert manifest.hook_attributes == {"active": False}


def test_callback_requires_the_exact_csrf_state() -> None:
    assert parse_callback("code=abc&state=expected", expected_state="expected") == "abc"
    with pytest.raises(ValueError, match="state"):
        parse_callback("code=abc&state=wrong", expected_state="expected")


def test_interrupted_secret_write_never_persists_the_private_key(tmp_path: Path) -> None:
    github = FakeGitHub(fail_secret="TOUCHSTONE_APP_PRIVATE_KEY")
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=github.opened.append,
        confirm_installation=lambda _url: False,
    )

    report = setup.run(SetupOptions())

    assert report.state == "partial"
    assert report.step == "private-key-repair-required"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "PRIVATE KEY" not in path.read_text(encoding="utf-8", errors="ignore")


def test_success_pipes_required_secrets_and_confirms_installation(tmp_path: Path) -> None:
    github = FakeGitHub()
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=github.opened.append,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions())

    assert report.state == "complete"
    assert set(github.secrets) == {
        "TOUCHSTONE_APP_ID",
        "TOUCHSTONE_APP_PRIVATE_KEY",
        "TOUCHSTONE_STATE_KEY",
    }
    assert github.secrets["TOUCHSTONE_APP_ID"] == b"42"
    assert github.opened == ["https://github.com/apps/acme-touchstone/installations/new"]
    state = (tmp_path / ".touchstone" / "state" / "actions-setup.json").read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in state
    assert "private" not in state.lower()


def test_check_mode_is_read_only(tmp_path: Path) -> None:
    github = FakeGitHub()
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: pytest.fail("must not create app"),
        exchange=lambda _code: pytest.fail("must not exchange code"),
        open_browser=lambda _url: pytest.fail("must not open browser"),
        confirm_installation=lambda _url: pytest.fail("must not prompt"),
    )

    report = setup.run(SetupOptions(check=True))

    assert report.state == "partial"
    assert report.step == "not-configured"
    assert list(tmp_path.rglob("*")) == []


def test_partial_setup_rerun_repairs_recoverable_secret_writes(tmp_path: Path) -> None:
    github = FakeGitHub(fail_secret="TOUCHSTONE_STATE_KEY")
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=github.opened.append,
        confirm_installation=lambda _url: True,
    )

    first = setup.run(SetupOptions())
    github.fail_secret = ""
    second = setup.run(SetupOptions())

    assert first.step == "state-key-secret"
    assert second.step == "private-key-repair-required"
    assert set(github.secrets) == {"TOUCHSTONE_APP_ID", "TOUCHSTONE_STATE_KEY"}


def test_setup_rejects_an_all_repositories_installation(tmp_path: Path) -> None:
    class AllRepositoriesGitHub(FakeGitHub):
        def installation(self):  # type: ignore[no-untyped-def]
            installation = super().installation()
            installation["repository_selection"] = "all"
            return installation

    github = AllRepositoriesGitHub()
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=github.opened.append,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions())

    assert report.state == "partial"
    assert report.step == "repository-scope-mismatch"
    assert "selected" in report.repair
