"""Render the repository-owned GitHub Actions execution backend."""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from touchstone.config import Config, ConfigError
from touchstone.hosted.snapshot import config_digest

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ACTION_API = "https://api.github.com/repos/Misoto22/touchstone"


@dataclass(frozen=True, slots=True)
class ActionPins:
    """Immutable upstream Action revisions audited by Touchstone."""

    checkout: str = "3d3c42e5aac5ba805825da76410c181273ba90b1"
    setup_python: str = "5fda3b95a4ea91299a34e894583c3862153e4b97"
    setup_node: str = "249970729cb0ef3589644e2896645e5dc5ba9c38"
    upload_artifact: str = "ea165f8d65b6e75b540449e92b4886f43607fa02"
    download_artifact: str = "634f93cb2916e3fdff6788551b99b062d0335ce0"
    app_token: str = "67018539274d69449ef7c02e8e71183d1719ab42"

    def __post_init__(self) -> None:
        for name, value in (
            ("checkout", self.checkout),
            ("setup_python", self.setup_python),
            ("setup_node", self.setup_node),
            ("upload_artifact", self.upload_artifact),
            ("download_artifact", self.download_artifact),
            ("app_token", self.app_token),
        ):
            _require_sha(value, f"actions pin {name}")


@dataclass(frozen=True, slots=True)
class ActionsDiff:
    """A read-only workflow comparison that writes only on explicit request."""

    path: Path
    rendered: str
    diff: str
    changed: bool

    def write(self) -> None:
        if not self.changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(self.rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _require_sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _SHA.fullmatch(normalized):
        raise ConfigError(f"{label} must be an immutable 40-character commit SHA")
    return normalized


def _cron(config: Config) -> str:
    minutes = config.actions.wake_minutes
    if minutes == 60:
        return "17 * * * *"
    if minutes not in {5, 10, 15, 20, 30}:
        raise ConfigError("hosted wake cadence must be one of 5, 10, 15, 20, 30, or 60 minutes")
    minute_field = ",".join(str(minute) for minute in range(7, 60, minutes))
    return f"{minute_field} * * * *"


def render_workflow(config: Config, pins: ActionPins, *, action_sha: str) -> str:
    """Render a least-privilege workflow with separate credential domains."""

    sha = _require_sha(action_sha, "first-party Action reference")
    cron = _cron(config)
    branch = config.forge.default_branch
    branch_expression = branch.replace("'", "''")
    retention = config.actions.artifact_retention_days
    model_secret = "OPENAI_API_KEY" if config.engine.name == "codex" else "ANTHROPIC_API_KEY"
    model_input = "openai-api-key" if config.engine.name == "codex" else "anthropic-api-key"
    analysis_candidate_artifact = (
        "touchstone-candidate-${{ steps.touchstone.outputs.candidate_id || github.run_id }}"
    )
    downstream_candidate_artifact = (
        "touchstone-candidate-${{ needs.analysis.outputs.candidate_id || github.run_id }}"
    )
    state_artifact = f"touchstone-state-{config_digest(config).removeprefix('sha256:')}"
    approval = config.actions.approval_environment
    if len(approval) > 255 or any(ord(character) < 32 for character in approval):
        raise ConfigError("actions.approval_environment contains unsupported characters")
    environment_block = (
        f"    environment:\n      name: {json.dumps(approval)}\n" if approval else ""
    )
    published_candidate = "${{ steps.touchstone.outputs.candidate_id }}"
    final_candidate = (
        "${{ needs.publish.outputs.candidate_id || needs.analysis.outputs.candidate_id }}"
    )
    final_change = (
        "${{ needs.publish.outputs.change_state || needs.analysis.outputs.change_state }}"
    )
    final_reason = "${{ needs.publish.outputs.reason_code || needs.analysis.outputs.reason_code }}"
    if not 1 <= retention <= 90:
        raise ConfigError("actions.artifact_retention_days must be between 1 and 90")
    if config.actions.auto_merge:
        raise ConfigError("hosted auto-merge is not supported; publication is PR-only")

    # Keep this YAML explicit. It is a security boundary reviewed and owned by
    # the consuming repository, not an opaque reusable workflow.
    return f"""# Generated by `touchstone actions init`. Review changes before committing.
name: Touchstone

on:
  schedule:
    - cron: '{cron}'
  workflow_dispatch:
    inputs:
      candidate_id:
        description: Existing candidate to resume
        required: false
        type: string
      decision:
        description: Resume decision
        required: false
        type: choice
        options:
          - approve
          - close
          - reanalyze

permissions:
  contents: read

concurrency:
  group: touchstone-${{{{ github.repository }}}}
  cancel-in-progress: false

jobs:
  prepare:
    if: github.ref == 'refs/heads/{branch_expression}'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read
    outputs:
      run_id: ${{{{ steps.touchstone.outputs.run_id }}}}
      should_run: ${{{{ steps.touchstone.outputs.should_run }}}}
    steps:
      - uses: actions/checkout@{pins.checkout}
        with:
          persist-credentials: false
      - uses: actions/setup-python@{pins.setup_python}
        with:
          python-version: '3.12'
      - id: touchstone
        uses: Misoto22/touchstone@{sha}
        with:
          stage: prepare
          github-token: ${{{{ github.token }}}}
          candidate-id: ${{{{ inputs.candidate_id }}}}
          decision: ${{{{ inputs.decision }}}}
      - uses: actions/upload-artifact@{pins.upload_artifact}
        if: always()
        with:
          name: touchstone-prepare-${{{{ github.run_id }}}}
          path: .touchstone/hosted/prepare
          if-no-files-found: error
          retention-days: {retention}

  analysis:
    needs: prepare
    if: needs.prepare.outputs.should_run == 'true'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read
    outputs:
      loop: ${{{{ steps.touchstone.outputs.loop }}}}
      candidate_id: ${{{{ steps.touchstone.outputs.candidate_id }}}}
      outcome: ${{{{ steps.touchstone.outputs.outcome }}}}
      change_state: ${{{{ steps.touchstone.outputs.change_state }}}}
      reason_code: ${{{{ steps.touchstone.outputs.reason_code }}}}
      partial: ${{{{ steps.touchstone.outputs.partial }}}}
    steps:
      - uses: actions/checkout@{pins.checkout}
        with:
          persist-credentials: false
      - uses: actions/setup-python@{pins.setup_python}
        with:
          python-version: '3.12'
      - uses: actions/setup-node@{pins.setup_node}
        with:
          node-version: '{config.actions.node_version}'
      - uses: actions/download-artifact@{pins.download_artifact}
        with:
          name: touchstone-prepare-${{{{ github.run_id }}}}
          path: .touchstone/hosted/prepare
      - id: touchstone
        uses: Misoto22/touchstone@{sha}
        with:
          stage: analysis
          {model_input}: ${{{{ secrets.{model_secret} }}}}
          state-key: ${{{{ secrets.TOUCHSTONE_STATE_KEY }}}}
          candidate-id: ${{{{ inputs.candidate_id }}}}
          decision: ${{{{ inputs.decision }}}}
      - uses: actions/upload-artifact@{pins.upload_artifact}
        if: always()
        with:
          name: {analysis_candidate_artifact}
          path: .touchstone/hosted/candidate
          if-no-files-found: error
          retention-days: {retention}

  verify:
    needs: analysis
    if: needs.analysis.outputs.outcome == 'proposed'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read
    outputs:
      outcome: ${{{{ steps.touchstone.outputs.outcome }}}}
      candidate_id: ${{{{ steps.touchstone.outputs.candidate_id }}}}
    steps:
      - uses: actions/checkout@{pins.checkout}
        with:
          persist-credentials: false
      - uses: actions/setup-python@{pins.setup_python}
        with:
          python-version: '3.12'
      - uses: actions/download-artifact@{pins.download_artifact}
        with:
          name: {downstream_candidate_artifact}
          path: .touchstone/hosted/candidate
      - name: Verify candidate without publishing credentials
        id: touchstone
        uses: Misoto22/touchstone@{sha}
        with:
          stage: verify
          github-token: ${{{{ github.token }}}}
          state-key: ${{{{ secrets.TOUCHSTONE_STATE_KEY }}}}
          candidate-id: ${{{{ inputs.candidate_id }}}}
          expected-candidate-id: ${{{{ needs.analysis.outputs.candidate_id }}}}
          expected-loop: ${{{{ needs.analysis.outputs.loop }}}}
          decision: ${{{{ inputs.decision }}}}
      - uses: actions/upload-artifact@{pins.upload_artifact}
        if: always()
        with:
          name: touchstone-verified-${{{{ github.run_id }}}}
          path: .touchstone/hosted/verified
          if-no-files-found: error
          retention-days: {retention}

  publish:
    needs:
      - analysis
      - verify
    if: >-
      always() &&
      needs.analysis.result != 'cancelled' &&
      (needs.analysis.outputs.outcome == 'proposed' || inputs.decision == 'reanalyze') &&
      (needs.analysis.outputs.outcome != 'proposed' || needs.verify.result == 'success')
    runs-on: ubuntu-latest
{environment_block}    permissions:
      contents: read
      actions: read
    outputs:
      change_state: ${{{{ steps.touchstone.outputs.change_state }}}}
      outcome: ${{{{ steps.touchstone.outputs.outcome }}}}
      partial: ${{{{ steps.touchstone.outputs.partial }}}}
      candidate_id: {published_candidate}
      reason_code: ${{{{ steps.touchstone.outputs.reason_code }}}}
    steps:
      - uses: actions/checkout@{pins.checkout}
        with:
          persist-credentials: false
      - uses: actions/setup-python@{pins.setup_python}
        with:
          python-version: '3.12'
      - uses: actions/download-artifact@{pins.download_artifact}
        with:
          name: {downstream_candidate_artifact}
          path: .touchstone/hosted/candidate
      - uses: actions/download-artifact@{pins.download_artifact}
        if: needs.verify.result == 'success'
        with:
          name: touchstone-verified-${{{{ github.run_id }}}}
          path: .touchstone/hosted/verified
      - id: app-token
        uses: actions/create-github-app-token@{pins.app_token}
        with:
          app-id: ${{{{ secrets.TOUCHSTONE_APP_ID }}}}
          private-key: ${{{{ secrets.TOUCHSTONE_APP_PRIVATE_KEY }}}}
          owner: ${{{{ github.repository_owner }}}}
          repositories: ${{{{ github.event.repository.name }}}}
      - id: touchstone
        uses: Misoto22/touchstone@{sha}
        with:
          stage: publish
          github-token: ${{{{ steps.app-token.outputs.token }}}}
          state-key: ${{{{ secrets.TOUCHSTONE_STATE_KEY }}}}
          candidate-id: ${{{{ inputs.candidate_id }}}}
          expected-candidate-id: ${{{{ needs.analysis.outputs.candidate_id }}}}
          expected-loop: ${{{{ needs.analysis.outputs.loop }}}}
          decision: ${{{{ inputs.decision }}}}
      - uses: actions/upload-artifact@{pins.upload_artifact}
        if: always()
        with:
          name: touchstone-publish-${{{{ github.run_id }}}}
          path: .touchstone/hosted/publish
          if-no-files-found: error
          retention-days: {retention}

  snapshot:
    needs:
      - prepare
      - analysis
      - verify
      - publish
    if: always() && needs.prepare.result != 'skipped'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read
    steps:
      - uses: actions/checkout@{pins.checkout}
        with:
          persist-credentials: false
      - uses: actions/setup-python@{pins.setup_python}
        with:
          python-version: '3.12'
      - uses: actions/download-artifact@{pins.download_artifact}
        with:
          pattern: touchstone-*-${{{{ github.run_id }}}}
          path: .touchstone/hosted/inputs
          merge-multiple: true
      - uses: actions/download-artifact@{pins.download_artifact}
        if: needs.analysis.outputs.candidate_id != ''
        with:
          name: {downstream_candidate_artifact}
          path: .touchstone/hosted/inputs/candidate
      - id: touchstone
        uses: Misoto22/touchstone@{sha}
        with:
          stage: snapshot
          state-key: ${{{{ secrets.TOUCHSTONE_STATE_KEY }}}}
          final-outcome: ${{{{ needs.publish.outputs.outcome || needs.analysis.outputs.outcome }}}}
          final-candidate-id: {final_candidate}
          final-loop: ${{{{ needs.analysis.outputs.loop }}}}
          final-change-state: {final_change}
          final-reason-code: {final_reason}
          final-partial: ${{{{ needs.publish.outputs.partial || needs.analysis.outputs.partial }}}}
          publish-job-result: ${{{{ needs.publish.result }}}}
      - uses: actions/upload-artifact@{pins.upload_artifact}
        with:
          name: {state_artifact}
          path: .touchstone/hosted/snapshot
          if-no-files-found: error
          retention-days: {retention}
"""


def actions_diff(repo: Path, rendered: str) -> ActionsDiff:
    path = repo.resolve() / ".github" / "workflows" / "touchstone.yml"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = current != rendered
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    return ActionsDiff(path=path, rendered=rendered, diff=diff, changed=changed)


def installed_release_tag() -> str:
    """Name the release tag that matches the installed Touchstone distribution."""

    try:
        installed = version("touchstone-agent")
    except PackageNotFoundError as exc:
        raise ConfigError(
            "the installed Touchstone version is unknown; pass --action-sha with a "
            "40-character commit SHA or set actions.action_sha"
        ) from exc
    if not _RELEASE_VERSION.fullmatch(installed):
        raise ConfigError(
            f"the installed Touchstone version {installed!r} is not a published release; "
            "pass --action-sha with a 40-character commit SHA or set actions.action_sha"
        )
    return f"v{installed}"


def resolve_action_sha(config: Config, *, timeout: float = 10.0) -> str:
    """Resolve the installed release tag to an immutable Action commit.

    The default deliberately follows the installed distribution rather than the
    Action repository's moving default branch, so a generated workflow pins the
    revision whose behavior this CLI already documents.
    """

    if config.actions.action_sha:
        return _require_sha(config.actions.action_sha, "actions.action_sha")
    tag = installed_release_tag()
    request = urllib.request.Request(
        f"{_ACTION_API}/commits/{urllib.parse.quote(tag, safe='')}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "touchstone-agent"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
        raise ConfigError(
            f"could not resolve the Touchstone Action commit for {tag}; pass --action-sha "
            "with a 40-character commit SHA or set actions.action_sha"
        ) from exc
    return _require_sha(str(payload.get("sha", "")), "resolved Action reference")


__all__ = [
    "ActionPins",
    "ActionsDiff",
    "actions_diff",
    "installed_release_tag",
    "render_workflow",
    "resolve_action_sha",
]
