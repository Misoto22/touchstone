from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from touchstone.hosted.app_setup import (
    ActionsSetup,
    SetupOptions,
    build_manifest,
    parse_callback,
    permissions_are_exact,
    required_permissions,
)


class FakeGitHub:
    def __init__(self, *, fail_secret: str = "") -> None:
        self.fail_secret = fail_secret
        self.secrets: dict[str, bytes] = {}
        self.opened: list[str] = []
        self.installation_credentials: list[tuple[int, bytes]] = []
        self.scopes: list[tuple[str, bool]] = []
        self.listed: list[bool] = []
        self.deleted: list[str] = []

    def set_actions_secret(self, name: str, value: bytes, *, organization: bool = False) -> bool:
        if name == self.fail_secret:
            return False
        self.scopes.append((name, organization))
        self.secrets[name] = value
        return True

    def actions_secret_names(self, *, organization: bool = False) -> set[str]:
        self.listed.append(organization)
        return set(self.secrets)

    def delete_actions_secret(self, name: str, *, organization: bool = False) -> bool:
        self.deleted.append(name)
        self.secrets.pop(name, None)
        return True

    def installation(self, app_id: int, private_key: bytes):  # type: ignore[no-untyped-def]
        self.installation_credentials.append((app_id, private_key))
        return {
            "id": 7,
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
    # GitHub requires hook_attributes.url even for a webhook that is switched
    # off, and rejects the whole manifest without it.
    assert manifest.hook_attributes == {
        "active": False,
        "url": "https://github.com/Misoto22/touchstone",
    }


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
        confirm_installation=lambda _url: True,
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
    assert github.installation_credentials == [(42, _conversion()["pem"].encode())]
    state = (tmp_path / ".touchstone" / "state" / "actions-setup.json").read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in state
    assert "private" not in state.lower()
    assert '"installation_id": 7' in state

    checked = setup.run(SetupOptions(check=True))
    assert checked.state == "complete"
    assert checked.step == "configured-attested"
    assert "cached" in checked.repair
    assert len(github.installation_credentials) == 1


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
        def installation(self, app_id: int, private_key: bytes):  # type: ignore[no-untyped-def]
            installation = super().installation(app_id, private_key)
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


@pytest.mark.parametrize(
    "permissions",
    [
        pytest.param(
            {
                "actions": "read",
                "administration": "write",
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
            },
            id="extra-administration-write",
        ),
        pytest.param(
            {
                "actions": "write",
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
            },
            id="stronger-actions-access",
        ),
        pytest.param(
            {
                "actions": "read",
                "contents": "write",
                "issues": "write",
                "metadata": "write",
                "pull_requests": "write",
            },
            id="stronger-metadata-access",
        ),
        pytest.param(
            {"actions": "read", "contents": "write", "pull_requests": "write"},
            id="missing-issues",
        ),
    ],
)
def test_exact_permissions_reject_anything_broader_than_required(
    permissions: dict[str, str],
) -> None:
    assert permissions_are_exact(permissions) is False


@pytest.mark.parametrize(
    "permissions",
    [
        pytest.param(required_permissions(), id="exact"),
        pytest.param({**required_permissions(), "metadata": "read"}, id="implicit-metadata-read"),
    ],
)
def test_exact_permissions_accept_the_required_map(permissions: dict[str, str]) -> None:
    assert permissions_are_exact(permissions) is True


def test_setup_refuses_an_over_permissive_installation(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.installation = lambda _app_id, _key: {  # type: ignore[assignment]
        "id": 7,
        "app_id": 42,
        "repository_selection": "selected",
        "permissions": {**required_permissions(), "administration": "write"},
    }
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        exchange=lambda _code: _conversion(),
        code_provider=lambda _manifest, _state: "manifest-code",
        open_browser=lambda _url: None,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions())

    assert report.state == "partial"
    assert report.step == "permissions-mismatch"
    assert "exactly" in report.repair
    # Stored first so an unfinished install cannot consume the one-time key,
    # then taken back because this App is wrong rather than merely unfinished.
    assert github.deleted == ["TOUCHSTONE_APP_PRIVATE_KEY"]
    assert "TOUCHSTONE_APP_PRIVATE_KEY" not in github.secrets


def test_setup_persists_only_the_required_permission_map(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.installation = lambda _app_id, _key: {  # type: ignore[assignment]
        "id": 7,
        "app_id": 42,
        "repository_selection": "selected",
        "permissions": {**required_permissions(), "metadata": "read"},
    }
    config = _config(tmp_path)
    setup = ActionsSetup(
        config,
        github=github,
        exchange=lambda _code: _conversion(),
        code_provider=lambda _manifest, _state: "manifest-code",
        open_browser=lambda _url: None,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions())
    persisted = json.loads((config.state_dir / "actions-setup.json").read_text(encoding="utf-8"))

    assert report.state == "complete"
    assert persisted["permissions"] == required_permissions()


def test_a_user_owned_setup_writes_repository_secrets(tmp_path: Path) -> None:
    github = FakeGitHub()
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=lambda _url: None,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions(owner_type="user"))

    assert report.state == "complete"
    assert {name for name, _org in github.scopes} == {
        "TOUCHSTONE_APP_ID",
        "TOUCHSTONE_APP_PRIVATE_KEY",
        "TOUCHSTONE_STATE_KEY",
    }
    assert all(org is False for _name, org in github.scopes)


def test_an_organization_setup_writes_organization_secrets(tmp_path: Path) -> None:
    github = FakeGitHub()
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=lambda _url: None,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions(owner_type="organization"))

    assert report.state == "complete"
    assert all(org is True for _name, org in github.scopes)

    # A later --check must look for those secrets where setup put them.
    github.listed.clear()
    setup.run(SetupOptions(check=True, owner_type="organization"))
    assert github.listed and all(github.listed)


def test_an_organization_secret_stays_scoped_to_the_selected_repository() -> None:
    import subprocess

    from touchstone.hosted.github_api import GitHubCLI

    calls: list[list[str]] = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    github = GitHubCLI("acme/widgets", run=run)
    assert github.set_actions_secret("TOUCHSTONE_APP_ID", b"42", organization=True)

    argv = calls[0]
    assert "--org" in argv and argv[argv.index("--org") + 1] == "acme"
    assert "--visibility" in argv and argv[argv.index("--visibility") + 1] == "selected"
    assert "--repos" in argv and argv[argv.index("--repos") + 1] == "widgets"
    assert "--repo" not in argv
    assert b"42" not in b" ".join(a.encode() for a in argv)


def test_the_manifest_supplies_every_url_github_requires() -> None:
    """`url` and `hook_attributes.url` are the two mandatory manifest fields.

    Omitting the second made GitHub reject registration outright with
    `"url" wasn't supplied`, so no Owner App could ever be created.
    """
    import json

    manifest = json.loads(
        build_manifest(
            owner="acme",
            repository="widgets",
            redirect_url="http://127.0.0.1:8917/callback",
        ).to_json()
    )

    assert manifest["url"]
    assert manifest["hook_attributes"]["url"]
    assert manifest["hook_attributes"]["active"] is False


def test_the_csrf_state_travels_in_the_action_query_string(tmp_path: Path) -> None:
    """GitHub reads `state` from the URL and echoes it back to the redirect.

    Sent as a form field it never reached GitHub, so the callback carried no
    state and the exchange failed its own equality check.
    """
    import urllib.parse

    captured: list[str] = []

    class _Callback:
        def __init__(self, manifest, state, *, action, port):  # type: ignore[no-untyped-def]
            captured.append(action)
            self.start_url = "http://127.0.0.1:8917/"

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return False

        def wait(self, _timeout):  # type: ignore[no-untyped-def]
            return "manifest-code"

    from touchstone.hosted import app_setup

    setup = ActionsSetup(
        _config(tmp_path),
        github=FakeGitHub(),
        exchange=lambda _code: _conversion(),
        open_browser=lambda _url: None,
        confirm_installation=lambda _url: True,
    )
    setup._options = SetupOptions()
    original = app_setup._ManifestCallback
    app_setup._ManifestCallback = _Callback  # type: ignore[assignment]
    try:
        setup._browser_manifest_code(
            build_manifest(
                owner="acme",
                repository="widgets",
                redirect_url="http://127.0.0.1:8917/callback",
            ),
            "state-abc123",
        )
    finally:
        app_setup._ManifestCallback = original  # type: ignore[assignment]

    action = captured[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(action).query)
    assert query["state"] == ["state-abc123"]
    assert action.startswith("https://github.com/settings/apps/new?")


def test_an_unfinished_installation_does_not_consume_the_one_time_key(tmp_path: Path) -> None:
    """The manifest key exists once; an install still in progress must not cost it.

    GitHub cannot reissue it, so a lookup that simply found no installation yet
    leaves the stored secret alone and a rerun repairs from there.
    """
    github = FakeGitHub()
    github.installation = lambda _app_id, _key: None  # type: ignore[assignment]
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=lambda _url: None,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions())

    assert report.state == "partial"
    assert report.step == "installation-missing"
    assert github.deleted == []
    assert "TOUCHSTONE_APP_PRIVATE_KEY" in github.secrets


def test_a_repository_scope_that_is_too_broad_also_takes_the_key_back(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.installation = lambda _app_id, _key: {  # type: ignore[assignment]
        "id": 7,
        "app_id": 42,
        "repository_selection": "all",
        "permissions": required_permissions(),
    }
    setup = ActionsSetup(
        _config(tmp_path),
        github=github,
        code_provider=lambda _manifest, _state: "manifest-code",
        exchange=lambda _code: _conversion(),
        open_browser=lambda _url: None,
        confirm_installation=lambda _url: True,
    )

    report = setup.run(SetupOptions())

    assert report.step == "repository-scope-mismatch"
    assert github.deleted == ["TOUCHSTONE_APP_PRIVATE_KEY"]
