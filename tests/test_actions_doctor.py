from __future__ import annotations

import subprocess

from tests.test_doctor import MemoryForge, _v2_config
from touchstone.doctor import DoctorContext, run_doctor
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
                "permissions": {
                    "actions": "read",
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                }
            }

        def workflow(self, name: str = "touchstone.yml"):  # type: ignore[no-untyped-def]
            return {"name": name, "state": "active"}

        def repository_info(self):  # type: ignore[no-untyped-def]
            return {"private": False}

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
