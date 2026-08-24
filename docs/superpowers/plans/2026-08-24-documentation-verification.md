# Documentation and Release Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an accurate newcomer README and prove the complete Profile, local scheduler, and GitHub Actions paths from an installed wheel.

**Architecture:** README is generated only from implemented CLI/package evidence and follows the `docs:readme` canonical shape. Contract tests extract every documented command, build an isolated wheel, exercise representative fixture repositories, and statically verify the generated workflow before rendered GitHub review.

**Tech Stack:** Markdown, pytest, Hatchling, build, Twine, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-08-24-stack-profiles-actions-design.md`

## Global Constraints

- README remains English and answers what this is, whether it runs, and where to go next.
- Every command is copied from implemented CLI help and runs as written.
- No live App creation, secret write, PR creation, or auto-merge occurs in automated tests.
- Public/read-only facts are distinguished from mocked hosted integration.
- README rendered verification happens on the pushed implementation branch.
- Every behavior starts with a failing test and ends with a focused commit.

---

### Task 1: Installed-wheel end-to-end acceptance matrix

**Files:**
- Create: `tests/test_stack_actions_acceptance.py`
- Create: `tests/fixtures/acceptance/next-app/`
- Create: `tests/fixtures/acceptance/django-app/`
- Create: `tests/fixtures/acceptance/mixed-monorepo/`
- Modify: `tests/test_distribution.py`

**Interfaces:**
- Consumes: all public CLI commands from the three implementation plans
- Produces: isolated-wheel evidence for config, detection, run-due, workflow generation, and dry-run

- [ ] **Step 1: Write failing isolated-wheel scenarios**

```python
@pytest.mark.parametrize("fixture", ["next-app", "django-app", "mixed-monorepo"])
def test_installed_wheel_initializes_detected_repository(wheel_env: WheelEnv, fixture_repo: Path, fixture: str) -> None:
    result = wheel_env.run("touchstone", "init", "--non-interactive", "--engine", "codex", "--model", "test", "--workflow", "ci.yml", "--schedule", "hourly@00", cwd=fixture_repo / fixture)
    assert result.returncode == 0
    assert (fixture_repo / fixture / ".touchstone/generated.toml").exists()


def test_installed_wheel_renders_a_safe_actions_workflow(wheel_env: WheelEnv, next_repo: Path) -> None:
    result = wheel_env.run("touchstone", "actions", "init", "--action-sha", "a" * 40, cwd=next_repo)
    assert result.returncode == 0
    assert "pull_request:" not in (next_repo / ".github/workflows/touchstone.yml").read_text()
```

- [ ] **Step 2: Run tests and confirm the new public paths are incomplete**

Run: `.venv/bin/python -m pytest tests/test_stack_actions_acceptance.py tests/test_distribution.py -q`

Expected: FAIL until all commands exist in the built wheel.

- [ ] **Step 3: Build minimal representative fixtures**

Fixtures contain only valid manifests, lockfile evidence, minimal source files, Git metadata created by the test, and harmless fake CLI executables. The mixed fixture has Next.js web, Django API, and a shared TypeScript UI package with declared dependency edges.

- [ ] **Step 4: Exercise v1 migration and v2 refresh from the wheel**

Add scenarios for `config migrate --check`, explicit write with timezone/anchor, root override preservation, Profile drift exit 3, generated workflow drift exit 3, pure status, mocked reconcile, Clean Start, coalesced missed schedule, and dry-run publication suppression.

- [ ] **Step 5: Run full acceptance and distribution tests**

Run: `.venv/bin/python -m pytest tests/test_stack_actions_acceptance.py tests/test_distribution.py tests/test_acceptance.py -q`

Expected: PASS.

- [ ] **Step 6: Commit installed-wheel acceptance**

```bash
git add tests/test_stack_actions_acceptance.py tests/test_distribution.py tests/fixtures/acceptance
git commit -m "test: cover stack and hosted onboarding"
```

### Task 2: Rewrite README from verified product evidence

**Files:**
- Modify: `README.md`
- Modify: `tests/test_readme_commands.py`
- Link: `CONTEXT.md`
- Link: `docs/adr/`
- Link: `docs/superpowers/specs/2026-08-24-stack-profiles-actions-design.md`

**Interfaces:**
- Consumes: actual `touchstone --help`, generated config, generated workflow, doctor output, and acceptance commands

- [ ] **Step 1: Write a failing README contract test**

```python
def test_readme_documents_both_verified_onboarding_paths(readme: str) -> None:
    assert "pipx install touchstone-agent" in readme
    assert "touchstone profile detect" in readme
    assert "touchstone actions init" in readme
    assert "touchstone actions setup" in readme
    assert "touchstone doctor --actions" in readme


def test_every_touchstone_command_in_readme_parses(installed_touchstone: Path, readme: str) -> None:
    for argv in extracted_touchstone_commands(readme):
        assert cli_accepts(installed_touchstone, argv), argv
```

- [ ] **Step 2: Run README tests and record the stale sections**

Run: `.venv/bin/python -m pytest tests/test_readme_commands.py -q`

Expected: FAIL because Stack Profiles and hosted setup are not documented.

- [ ] **Step 3: Rewrite in the required canonical order**

Use: title/centered purpose and issue link; Features; Tech Stack; Project Structure; Getting Started; Documentation; GitHub Actions; Configuration and Profiles; Scheduling and Recovery; Safety Boundary; Development and Release; one-line license footer. Keep the first pasteable local path before any details, put the hosted alternative in one `<details>` block, and use one warning for Owner App/browser/secret setup.

- [ ] **Step 4: Document exact boundaries and recovery behavior**

State the nine Profiles, evidence/candidate behavior, Target/workspace model, generated-vs-project files, disabled Validation Candidates, PR-only default, App ownership, two browser confirmations, standard secret names, encrypted 90-day artifacts, public/private wake defaults, latency/missed-run coalescing, 60-day public inactivity warning, structured resume, exit codes, v1 migration, and no remote Profile/PAT/PR-trigger defaults.

- [ ] **Step 5: Run README and package command tests**

Run: `.venv/bin/python -m pytest tests/test_readme_commands.py tests/test_stack_actions_acceptance.py tests/test_distribution.py -q`

Expected: PASS with every command copied from actual help.

- [ ] **Step 6: Commit README**

```bash
git add README.md tests/test_readme_commands.py
git commit -m "docs: document profiles and hosted execution"
```

### Task 3: Full verification, review, and branch publication

**Files:**
- Modify only when a verification failure has a root-cause fix in its owning task
- Verify: `.github/workflows/ci.yml`
- Verify: `.github/workflows/release.yml`
- Verify: `pyproject.toml`

**Interfaces:**
- Produces: a reviewable branch with all gates green and no unsupported completion claim

- [ ] **Step 1: Run the complete source test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: PASS.

- [ ] **Step 2: Run lint and graph consistency**

Run: `.venv/bin/ruff check . && .venv/bin/python -m touchstone.cli graph --check`

Expected: both commands exit 0.

- [ ] **Step 3: Build and inspect distributions**

Run: `.venv/bin/python -m build && .venv/bin/python -m twine check dist/*`

Expected: wheel and sdist build; Twine reports both valid.

- [ ] **Step 4: Run isolated-wheel and secret/static workflow audits**

Run: `.venv/bin/python -m pytest tests/test_distribution.py tests/test_stack_actions_acceptance.py tests/test_actions_workflow.py tests/test_hosted_crypto.py -q`

Expected: PASS. Then inspect `git diff --check`, `git status --short`, workflow permissions, every `uses:` SHA, and repository secret-marker scans without printing values.

- [ ] **Step 5: Push the implementation branch and verify CI**

```bash
git push -u origin codex/stack-profiles-actions
gh run list --branch codex/stack-profiles-actions --limit 5
```

Wait for the exact branch CI run. Do not claim hosted execution works merely because unit tests pass.

- [ ] **Step 6: Render-check README on GitHub**

Open `https://github.com/Misoto22/touchstone/blob/codex/stack-profiles-actions/README.md`. Verify every image has non-zero natural width, centered text does not wrap, tables do not wrap mid-cell, links resolve, Mermaid renders if present, and both themes remain readable.

- [ ] **Step 7: Commit only evidence-backed corrections**

If verification required a correction, rerun its owning focused tests plus the full suite, then commit with the appropriate `fix:`, `test:`, or `docs:` subject. Leave the branch ready for independent review; do not merge or release without a separate user authorization.
