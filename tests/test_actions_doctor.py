from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.test_doctor import MemoryForge, _v2_config
from touchstone.doctor import DoctorContext, run_doctor
from touchstone.hosted.app_setup import required_permissions
from touchstone.hosted.github_api import GitHubCLI
from touchstone.hosted.workflow import ActionPins, actions_diff, render_workflow


def test_secret_values_are_passed_only_through_stdin() -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    github = GitHubCLI("acme/widgets", run=run)
    pem = b"-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----\n"

    assert github.set_actions_secret("TOUCHSTONE_APP_PRIVATE_KEY", pem)
    argv, standard_input = calls[0]
    assert argv == [
        "gh",
        "secret",
        "set",
        "TOUCHSTONE_APP_PRIVATE_KEY",
        "--app",
        "actions",
        "--repo",
        "acme/widgets",
    ]
    assert standard_input == pem
    assert all("PRIVATE KEY" not in argument for argument in argv)


def test_repository_installation_uses_an_app_jwt_without_exposing_it_to_gh() -> None:  # type: ignore[no-untyped-def]
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "id": 7,
                    "app_id": 42,
                    "repository_selection": "selected",
                    "permissions": {"contents": "write"},
                }
            ).encode()

    def open_url(request, **_kwargs):  # type: ignore[no-untyped-def]
        requests.append(request)
        return Response()

    github = GitHubCLI(
        "acme/widgets",
        run=lambda *_args, **_kwargs: pytest.fail("App verification must not call gh"),
        open_url=open_url,
    )

    installation = github.installation(42, pem)

    assert installation is not None and installation["app_id"] == 42
    authorization = requests[0].headers["Authorization"]
    assert authorization.startswith("Bearer ")
    assert pem.decode() not in authorization


def test_actions_doctor_checks_workflow_app_and_secret_metadata(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _report, config = _v2_config(tmp_path)
    rendered = render_workflow(config, ActionPins(), action_sha="a" * 40)
    actions_diff(config.repo_path, rendered).write()

    class HostedGitHub:
        def actions_secret_names(self) -> set[str]:
            return {
                "OPENAI_API_KEY",
                "TOUCHSTONE_APP_ID",
                "TOUCHSTONE_APP_PRIVATE_KEY",
                "TOUCHSTONE_STATE_KEY",
            }

        def installation(self):  # type: ignore[no-untyped-def]
            return {
                "repository_selection": "selected",
                "permissions": {
                    "actions": "read",
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                },
            }

        def workflow(self, name: str = "touchstone.yml"):  # type: ignore[no-untyped-def]
            return {"name": name, "state": "active"}

        def repository_info(self):  # type: ignore[no-untyped-def]
            return {"private": False, "pushed_at": datetime.now(UTC).isoformat()}

        def environment(self, name: str):  # type: ignore[no-untyped-def]
            return {"name": name}

    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        online=True,
        actions=HostedGitHub(),
    )

    report = run_doctor(config, context)

    assert report.by_id("actions.workflow").level == "PASS"
    assert report.by_id("actions.pins").level == "PASS"
    assert report.by_id("actions.secrets").level == "PASS"
    assert report.by_id("actions.app").level == "PASS"
    assert report.by_id("actions.visibility").level == "PASS"
    assert report.by_id("actions.schedule_inactivity").level == "PASS"


def test_doctor_labels_cached_app_setup_as_unverified_live_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _report, config = _v2_config(tmp_path)
    actions_diff(
        config.repo_path,
        render_workflow(config, ActionPins(), action_sha="a" * 40),
    ).write()
    state = config.state_dir / "actions-setup.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "repository": config.forge.slug,
                "state": "complete",
                "step": "configured",
                "app_id": 42,
                "app_slug": "acme-touchstone",
                "installation_id": 7,
                "repository_selection": "selected",
                "permissions": {
                    "actions": "read",
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                },
                "updated_at": "2026-08-24T12:00:00Z",
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    actions = SimpleNamespace(
        actions_secret_names=lambda: {
            "OPENAI_API_KEY",
            "TOUCHSTONE_APP_ID",
            "TOUCHSTONE_APP_PRIVATE_KEY",
            "TOUCHSTONE_STATE_KEY",
        },
        installation=lambda: None,
        workflow=lambda name="touchstone.yml": {"name": name, "state": "active"},
        repository_info=lambda: {
            "private": False,
            "pushed_at": datetime.now(UTC).isoformat(),
        },
        environment=lambda _name: None,
    )
    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        online=True,
        actions=actions,
    )

    check = run_doctor(config, context).by_id("actions.app")

    assert check.level == "WARN"
    assert "live state was not reverified" in check.summary


def test_actions_doctor_warns_when_a_public_repository_is_near_the_cutoff(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    _report, config = _v2_config(tmp_path)
    rendered = render_workflow(config, ActionPins(), action_sha="a" * 40)
    actions_diff(config.repo_path, rendered).write()

    class InactiveGitHub:
        def actions_secret_names(self) -> set[str]:
            return {
                "OPENAI_API_KEY",
                "TOUCHSTONE_APP_ID",
                "TOUCHSTONE_APP_PRIVATE_KEY",
                "TOUCHSTONE_STATE_KEY",
            }

        def installation(self):  # type: ignore[no-untyped-def]
            return {
                "repository_selection": "selected",
                "permissions": {
                    "actions": "read",
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                },
            }

        def workflow(self, name: str = "touchstone.yml"):  # type: ignore[no-untyped-def]
            return {"name": name, "state": "active"}

        def repository_info(self):  # type: ignore[no-untyped-def]
            return {"private": False, "pushed_at": "2000-01-01T00:00:00Z"}

        def environment(self, name: str):  # type: ignore[no-untyped-def]
            return {"name": name}

    context = DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        online=True,
        actions=InactiveGitHub(),
    )

    report = run_doctor(config, context)
    check = report.by_id("actions.schedule_inactivity")

    assert check.level == "WARN"
    assert "60-day" in check.summary
    assert "workflow_dispatch" in check.repair


def _attested_doctor_context(config, permissions: dict[str, str]):  # type: ignore[no-untyped-def]
    state = config.state_dir / "actions-setup.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "repository": config.forge.slug,
                "state": "complete",
                "step": "configured",
                "app_id": 42,
                "app_slug": "acme-touchstone",
                "installation_id": 7,
                "repository_selection": "selected",
                "permissions": permissions,
                "updated_at": "2026-08-24T12:00:00Z",
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    actions = SimpleNamespace(
        actions_secret_names=lambda: {
            "OPENAI_API_KEY",
            "TOUCHSTONE_APP_ID",
            "TOUCHSTONE_APP_PRIVATE_KEY",
            "TOUCHSTONE_STATE_KEY",
        },
        installation=lambda: None,
        workflow=lambda name="touchstone.yml": {"name": name, "state": "active"},
        repository_info=lambda: {
            "private": False,
            "pushed_at": datetime.now(UTC).isoformat(),
        },
        environment=lambda _name: None,
    )
    return DoctorContext(
        commands=frozenset({"git", "gh", "codex"}),
        forge=MemoryForge(labels={"touchstone:audit", "touchstone:needs-review"}),
        scheduler="launchd",
        online=True,
        actions=actions,
    )


def test_doctor_fails_an_app_installation_with_extra_permissions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _report, config = _v2_config(tmp_path)
    actions_diff(
        config.repo_path,
        render_workflow(config, ActionPins(), action_sha="a" * 40),
    ).write()
    context = _attested_doctor_context(
        config,
        {**required_permissions(), "administration": "write"},
    )

    check = run_doctor(config, context).by_id("actions.app")

    assert check.level == "FAIL"
    assert "do not match exactly" in check.summary


def test_doctor_accepts_githubs_implicit_metadata_read(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _report, config = _v2_config(tmp_path)
    actions_diff(
        config.repo_path,
        render_workflow(config, ActionPins(), action_sha="a" * 40),
    ).write()
    context = _attested_doctor_context(
        config,
        {**required_permissions(), "metadata": "read"},
    )

    check = run_doctor(config, context).by_id("actions.app")

    assert check.level == "WARN"
    assert "exact required scope" in check.summary
