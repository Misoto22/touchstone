from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_documented_first_run_commands_exist() -> None:
    result = subprocess.run(
        [str(Path(sys.executable).parent / "touchstone"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    for command in (
        "init",
        "profile",
        "doctor",
        "setup",
        "actions",
        "run",
        "run-due",
        "status",
        "install-scheduler",
    ):
        assert command in result.stdout


def test_readme_starts_with_the_installable_first_run() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = readme.split("### Getting Started", 1)[1].split("\n---\n", 1)[0]
    commands = [
        "pipx install touchstone-agent",
        "touchstone init",
        "touchstone doctor",
        "touchstone setup",
        "touchstone run code --dry-run",
    ]

    positions = [getting_started.index(command) for command in commands]
    assert positions == sorted(positions)
    assert getting_started.count("touchstone doctor") == 2
    assert "touchstone install-scheduler" not in getting_started


def test_readme_documents_profiles_and_both_execution_backends() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for command in (
        "touchstone profile detect",
        "touchstone profile refresh --check",
        "touchstone actions init",
        "touchstone actions setup",
        "touchstone actions setup --check",
        "touchstone run-due",
    ):
        assert command in readme
    for profile in (
        "generic",
        "javascript",
        "node",
        "typescript",
        "react",
        "nextjs",
        "python",
        "fastapi",
        "django",
    ):
        assert f"`{profile}`" in readme


def test_readme_uses_the_current_pr_only_resume_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "approve|close|reanalyze" in readme
    assert "resume <thread-id> merge" not in readme
    assert "enable auto-merge" not in readme.lower()
    # Auto-merge is a per-Loop opt-in rather than a thing Touchstone never
    # does, so the README has to carry both halves of the contract: off unless
    # a Loop asks, and refused outright where Verify is not independent.
    assert "auto-merge is off unless a loop enables it" in readme.lower()
    assert "policy-unsupported" in readme


def test_readme_links_architecture_and_operator_context() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for target in (
        "CONTEXT.md",
        "docs/adr/",
        "docs/superpowers/specs/2026-08-24-stack-profiles-actions-design.md",
    ):
        url = f"https://github.com/Misoto22/touchstone/blob/main/{target}"
        if target.endswith("/"):
            url = f"https://github.com/Misoto22/touchstone/tree/main/{target}"
        assert url in readme


def test_readme_links_the_published_release() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]

    assert "https://pypi.org/project/touchstone-agent/" in readme
    assert f"https://github.com/Misoto22/touchstone/releases/tag/v{version}" in readme
    assert "release candidate" not in readme
    assert "Before the first PyPI release" not in readme


def test_readme_resources_work_outside_github() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"]\(([^)\s]+)\)", readme)
    targets.extend(re.findall(r'\b(?:src|srcset)="([^"]+)"', readme))
    relative_targets = sorted(
        target
        for target in targets
        if not target.startswith(("https://", "http://", "#", "mailto:"))
    )

    assert relative_targets
    for target in relative_targets:
        assert (ROOT / target).is_file(), f"README resource does not exist: {target}"


def test_public_policy_files_are_linked_from_the_readme() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for path in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md"):
        assert (ROOT / path).is_file()
        assert f"](https://github.com/Misoto22/touchstone/blob/main/{path})" in readme


def test_readme_states_the_hosted_credential_boundary_exactly() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lowered = readme.lower()

    # Verify does hold the state-decryption key and a repository read token, so
    # it must never be described as credential-free or read-token-only.
    assert "credential-free verify" not in lowered
    assert "only a read token" not in lowered
    assert (
        "Verify runs on a separate runner that holds no model credential and "
        "no publishing credential" in readme
    )
    assert "it does receive a repository read token and the state-decryption key" in readme
    assert "Locked preparation and Validation Gates run as subprocesses with a scrubbed" in readme
    assert "project code never sees the state key or any token" in readme
    # Health checks call `gh`, so the README must not claim they are scrubbed.
    assert "Health checks are the deliberate exception" in readme
    assert "dependencies are only ever installed before model credentials exist" in readme


def test_readme_states_the_default_action_reference_and_manager_behaviour() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert f"`v{metadata['project']['version']}` today" in readme
    assert "rather than a moving branch" in readme
    assert "package name in `package.json` or `pyproject.toml`" in readme
    for command in (
        "npm ci --ignore-scripts",
        "pnpm install --frozen-lockfile --ignore-scripts",
        "yarn install --frozen-lockfile --ignore-scripts",
        "yarn install --immutable --mode=skip-build",
        "bun install --frozen-lockfile --ignore-scripts",
        "uv sync --frozen --no-install-workspace --no-build",
        "PDM_ONLY_BINARY=:all: pdm sync --frozen-lockfile --no-self",
        "pnpm run test",
        "bun x tsc",
    ):
        assert command in readme
    assert "policy-unsupported" in readme


def test_readme_does_not_document_removed_configuration_keys() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "codex_cli_version" not in readme
    assert "claude_code_version" not in readme


def test_readme_states_that_schema_v1_keeps_working() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "An existing schema-v1 configuration keeps loading unchanged" in readme
    assert "~/.config/touchstone/config.toml" in readme
    assert "upgrading is an explicit command" in readme


def test_readme_claims_no_more_hosted_evidence_than_exists() -> None:
    """The original brief: never claim a workflow ran in GitHub from local tests alone.

    Every stage has since run on a real runner, so the README may now say so. The
    guard moves with the evidence rather than retiring: what remains covered by
    tests alone must still be named as such.
    """
    # Compare against unwrapped prose: the statement is a wrapped blockquote, so
    # strip the quote markers before collapsing whitespace.
    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    readme = " ".join(line.lstrip("> ") for line in raw.splitlines())
    readme = " ".join(readme.split())

    assert "The whole hosted backend has now run on GitHub Actions runners" in readme
    # Partial-Publish recovery is the part no real run has exercised, so the
    # README must keep saying that instead of folding it into the claim above.
    assert (
        "Recovery from a Publish that fails partway is covered by tests but has not been"
        " exercised against a real interrupted run" in readme
    )
    assert "publication as unproven" not in readme
