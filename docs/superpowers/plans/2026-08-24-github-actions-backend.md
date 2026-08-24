# GitHub Actions Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GitHub-hosted Actions a first-class, resumable, least-privilege Execution Backend with generated workflow files and owner-controlled GitHub App publication.

**Architecture:** A repository-owned workflow defines every trigger, job, permission, concurrency group, and secret mapping. Pinned composite Actions encapsulate mechanics. Encrypted immutable artifacts move state and reviewed candidate data across jobs and workflow runs; an owner-controlled App supplies a short-lived publish identity only inside the Publish Stage.

**Tech Stack:** GitHub Actions YAML, Python 3.12+, `cryptography` AES-GCM, GitHub REST API, GitHub CLI, pytest

**Spec:** `docs/superpowers/specs/2026-08-24-stack-profiles-actions-design.md`

## Global Constraints

- Hosted execution triggers only from default-branch `schedule` and `workflow_dispatch`.
- Model credentials and publishing credentials never coexist in one job or step.
- Every third-party Action reference is an immutable 40-character commit SHA.
- Artifact plaintext contains no prompt, model output, patch, checkpoint, source file, or secret.
- The default may create a PR but never enables auto-merge.
- GitHub App creation/installation always includes visible browser confirmation.
- Every production behavior starts with a failing test and ends with a focused commit.

---

### Task 1: Encrypted State Snapshot and candidate bundles

**Files:**
- Create: `src/touchstone/hosted/__init__.py`
- Create: `src/touchstone/hosted/crypto.py`
- Create: `src/touchstone/hosted/snapshot.py`
- Modify: `pyproject.toml`
- Test: `tests/test_hosted_crypto.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Produces: `encrypt_bundle(manifest: BundleManifest, files: Mapping[str, Path], key: bytes) -> EncryptedBundle`
- Produces: `decrypt_bundle(bundle: EncryptedBundle, key: bytes, destination: Path) -> BundleManifest`
- Produces: `snapshot_state(config: Config, run: RunResult) -> SnapshotPlan`

- [ ] **Step 1: Write failing round-trip, tamper, and content tests**

```python
def test_snapshot_round_trip_uses_authenticated_encryption(tmp_path: Path) -> None:
    bundle = encrypt_bundle(manifest(), {"events.jsonl": write_secret_state(tmp_path)}, key())
    restored = decrypt_bundle(bundle, key(), tmp_path / "restored")
    assert restored.lineage == manifest().lineage
    assert (tmp_path / "restored/events.jsonl").read_text() == "private state"


def test_tampered_ciphertext_is_rejected(tmp_path: Path) -> None:
    bundle = corrupt(encrypt_bundle(manifest(), files(tmp_path), key()))
    with pytest.raises(BundleIntegrityError):
        decrypt_bundle(bundle, key(), tmp_path / "restored")
```

Test invalid key length, nonce reuse prevention, manifest additional-authenticated-data binding, archive path traversal, symlink rejection, config/Profile digest mismatch, repository/Loop mismatch, missing file, SQLite snapshot consistency, and a plaintext scan proving secret/sample prompt strings are absent.

- [ ] **Step 2: Run tests and verify hosted crypto is absent**

Run: `.venv/bin/python -m pytest tests/test_hosted_crypto.py tests/test_snapshot.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement AES-256-GCM bundles**

Decode `TOUCHSTONE_STATE_KEY` as exactly 32 random bytes from URL-safe base64. Build a deterministic tar payload in memory from an allowlist under `state_dir`; reject absolute paths, `..`, symlinks, devices, and unexpected members. Use a fresh 96-bit nonce and bind canonical JSON manifest bytes as AAD. Store manifest, nonce, ciphertext, and SHA-256 ciphertext digest.

- [ ] **Step 4: Add snapshot selection and Clean Start**

Select only a completed/held historical workflow run whose manifest matches repository, Loop, schema, config/Profile digest, and lineage. Missing, expired, deleted, or incompatible artifacts return a typed Clean Start reason; they never fall through to an unverified restore.

- [ ] **Step 5: Run crypto, event, ledger, and package tests**

Run: `.venv/bin/python -m pytest tests/test_hosted_crypto.py tests/test_snapshot.py tests/test_events.py tests/test_ledger.py tests/test_distribution.py -q`

Expected: PASS.

- [ ] **Step 6: Commit encrypted bundles**

```bash
git add src/touchstone/hosted pyproject.toml tests/test_hosted_crypto.py tests/test_snapshot.py
git commit -m "feat: encrypt hosted state snapshots"
```

### Task 2: Repository-owned workflow and pinned composite Action

**Files:**
- Create: `action.yml`
- Create: `scripts/action-entrypoint.sh`
- Create: `src/touchstone/hosted/workflow.py`
- Modify: `src/touchstone/cli.py`
- Test: `tests/test_actions_workflow.py`
- Test: `tests/test_action_contract.py`

**Interfaces:**
- Produces: `render_workflow(config: Config, pins: ActionPins) -> str`
- Produces: `actions_diff(repo: Path, rendered: str) -> ActionsDiff`
- Produces: CLI `touchstone actions init [--check] [--action-sha SHA]`

- [ ] **Step 1: Write failing workflow security assertions**

```python
def test_generated_workflow_exposes_trust_boundaries(config: Config) -> None:
    workflow = yaml.safe_load(render_workflow(config, pins()))
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "TOUCHSTONE_APP_PRIVATE_KEY" not in str(workflow["jobs"]["analysis"])
    assert "OPENAI_API_KEY" not in str(workflow["jobs"]["publish"])


def test_every_uses_reference_is_an_immutable_sha(config: Config) -> None:
    text = render_workflow(config, pins())
    assert not re.search(r"uses:.*@(main|master|v\d+)$", text, re.MULTILINE)
    assert all(len(ref) == 40 for ref in extract_action_refs(text))
```

Test public 15-minute and private 60-minute off-hour cron, explicit minimal permissions, one repository concurrency group, `queue: single` behavior documentation, default-branch dispatch validation, bounded inputs, PR-only default, artifact retention 90, job outputs, summary/annotation mapping, and no pull-request trigger.

- [ ] **Step 2: Run tests and confirm workflow rendering is absent**

Run: `.venv/bin/python -m pytest tests/test_actions_workflow.py tests/test_action_contract.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement workflow rendering and safe file ownership**

Write `.github/workflows/touchstone.yml` atomically only after printing a unified diff. `--check` exits 0 for exact match and 3 for drift. Require a 40-hex first-party Action SHA; interactive mode may resolve a release tag to a commit through `gh api`, while non-interactive mode requires the SHA explicitly.

- [ ] **Step 4: Implement the composite Action stages**

`action.yml` accepts an enumerated `stage` input (`prepare`, `analysis`, `publish`, `snapshot`) and a package version. The shell entrypoint uses `set -euo pipefail`, installs `touchstone-agent==VERSION` in an isolated environment, and invokes only the matching CLI command. It cannot read the `secrets` context; callers pass exact step environment variables.

- [ ] **Step 5: Run workflow, CLI, and package tests**

Run: `.venv/bin/python -m pytest tests/test_actions_workflow.py tests/test_action_contract.py tests/test_readme_commands.py tests/test_distribution.py -q`

Expected: PASS.

- [ ] **Step 6: Commit generated Actions integration**

```bash
git add action.yml scripts/action-entrypoint.sh src/touchstone/hosted/workflow.py src/touchstone/cli.py tests/test_actions_workflow.py tests/test_action_contract.py
git commit -m "feat: generate the hosted workflow"
```

### Task 3: Hosted stage commands, artifact restore, and structured resume

**Files:**
- Create: `src/touchstone/hosted/runtime.py`
- Modify: `src/touchstone/runner.py`
- Modify: `src/touchstone/cli.py`
- Modify: `src/touchstone/doctor.py`
- Test: `tests/test_hosted_runtime.py`
- Test: `tests/test_hosted_resume.py`

**Interfaces:**
- Produces: CLI-internal `hosted prepare|analysis|publish|snapshot`
- Produces: `HostedOutputs.write(path: Path) -> None`
- Consumes: encrypted bundles, RunResult, Due Slot claim, Owner App token environment

- [ ] **Step 1: Write failing cross-stage tests**

```python
def test_analysis_stage_cannot_publish(hosted: HostedHarness) -> None:
    result = hosted.analysis(env={"OPENAI_API_KEY": "model"})
    assert result.candidate_bundle
    assert hosted.forge.mutations == []


def test_publish_verifies_candidate_before_mutation(hosted: HostedHarness) -> None:
    bundle = hosted.tampered_candidate()
    result = hosted.publish(bundle, env={"GH_TOKEN": "app-token"})
    assert result.outcome == "blocked"
    assert hosted.forge.mutations == []
```

Test exact lineage/candidate/reviewed-head binding, prepare without secrets, analysis without write token, publish without model key, App token expiry handling, outcome→exit code mapping, `awaiting_human`/`awaiting_checks` outputs, partial failure preservation, snapshot on completed/blocked/failed, and Clean Start warning.

- [ ] **Step 2: Run tests and confirm hosted stage commands are absent**

Run: `.venv/bin/python -m pytest tests/test_hosted_runtime.py tests/test_hosted_resume.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement explicit stage entrypoints**

Each stage validates an allowlist of expected environment variables and rejects prohibited credentials. Write versioned JSON outputs and `$GITHUB_OUTPUT`/`$GITHUB_STEP_SUMMARY` when present. Never print secret values, decrypted state paths, full prompts, or model transcripts.

- [ ] **Step 4: Implement structured workflow dispatch resume**

Resume inputs are candidate ID plus `approve`, `close`, or `reanalyze`. Restore the exact Snapshot Lineage. Approve reruns health/Validation Gates and marks the draft ready; close records closed; reanalyze archives the old candidate and creates a manual Due Slot from current default-branch state.

- [ ] **Step 5: Run hosted, resume, publication, and outcome tests**

Run: `.venv/bin/python -m pytest tests/test_hosted_runtime.py tests/test_hosted_resume.py tests/test_snapshot.py tests/test_resume.py tests/test_publication.py tests/test_outcomes.py -q`

Expected: PASS.

- [ ] **Step 6: Commit hosted lifecycle stages**

```bash
git add src/touchstone/hosted/runtime.py src/touchstone/runner.py src/touchstone/cli.py src/touchstone/doctor.py tests/test_hosted_runtime.py tests/test_hosted_resume.py
git commit -m "feat: run split hosted trust stages"
```

### Task 4: Resumable GitHub App Manifest setup

**Files:**
- Create: `src/touchstone/hosted/app_setup.py`
- Create: `src/touchstone/hosted/github_api.py`
- Modify: `src/touchstone/cli.py`
- Modify: `src/touchstone/doctor.py`
- Test: `tests/test_app_setup.py`
- Test: `tests/test_actions_doctor.py`

**Interfaces:**
- Produces: `ActionsSetup.run(options: SetupOptions) -> SetupReport`
- Produces: `AppManifest`, `PartialSetup`, `SetupStep`
- Produces: CLI `touchstone actions setup [--check]`

- [ ] **Step 1: Write failing manifest and interruption tests**

```python
def test_manifest_uses_least_privilege_permissions() -> None:
    manifest = build_manifest(owner="Misoto22", repository="touchstone", redirect_url="http://127.0.0.1:8917/callback")
    assert manifest.permissions == {
        "contents": "write",
        "pull_requests": "write",
        "actions": "read",
        "issues": "write",
    }


def test_interrupted_secret_write_never_persists_pem(tmp_path: Path, setup: SetupHarness) -> None:
    setup.fail_secret_write_once()
    report = setup.run()
    assert report.state == "partial"
    assert not any("PRIVATE KEY" in p.read_text(errors="ignore") for p in tmp_path.rglob("*" ) if p.is_file())
```

Test CSRF state validation, one-hour manifest-code expiry, loopback callback, manual code fallback, owner type URLs, create/install browser steps, one-time conversion, stdin-only secret writes, state-key generation, idempotent re-run, App exists/not installed, installation repo mismatch, replacement-key repair, no `gh app` assumption, and `--check` read-only behavior.

- [ ] **Step 2: Run tests and verify setup orchestration is absent**

Run: `.venv/bin/python -m pytest tests/test_app_setup.py tests/test_actions_doctor.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement the Manifest state machine**

Use `secrets.token_urlsafe` for state, `http.server.ThreadingHTTPServer` bound to loopback, `webbrowser.open`, `urllib.request` for conversion, and bounded timeouts. Keep PEM as bytes in memory, retry `gh secret set ... --body -` within the process, overwrite the byte buffer after use, and never include it in reports/exceptions.

- [ ] **Step 4: Provision portable secrets and optional Environment**

Create a random 32-byte state key and pipe its encoded value to `gh secret set TOUCHSTONE_STATE_KEY`. Store provider keys only when supplied interactively through stdin. Store App client ID and private key under standard names. Optional Approval-Gated Mode creates/configures a publish Environment only after explicit confirmation and plan-capability checks.

- [ ] **Step 5: Add Actions doctor checks**

Check workflow presence/drift/enabled state, immutable pins, repository visibility/cadence, default branch, App installation/permissions/repository scope, secret metadata presence without reading values, Environment capability, artifact retention, required workflows, inactivity warning, and Partial Setup repair commands.

- [ ] **Step 6: Run setup and full doctor tests**

Run: `.venv/bin/python -m pytest tests/test_app_setup.py tests/test_actions_doctor.py tests/test_doctor.py tests/test_setup.py -q`

Expected: PASS.

- [ ] **Step 7: Commit Owner App setup**

```bash
git add src/touchstone/hosted/app_setup.py src/touchstone/hosted/github_api.py src/touchstone/cli.py src/touchstone/doctor.py tests/test_app_setup.py tests/test_actions_doctor.py
git commit -m "feat: guide owner app setup"
```
