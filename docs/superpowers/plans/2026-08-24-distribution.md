# Scheduling and Public Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship native scheduling, public repository standards, CI/release automation, and a README that carries a new user from installation to a verified scheduled dry run.

**Architecture:** A portable schedule parser feeds launchd and systemd adapters behind one scheduler interface. Packaging and repository automation verify the same installed-wheel path documented for users before the repository becomes public.

**Tech Stack:** Python 3.12+, launchd plist, systemd user units, GitHub Actions, Hatchling, build, Twine, pipx

**Spec:** `docs/superpowers/specs/2026-08-24-ready-to-use-design.md`

## Global Constraints

- Supported schedules are `hourly`, `daily@HH:MM`, and `weekly@DAY,HH:MM`.
- Scheduler files contain absolute executable/config paths and no credentials.
- Installation is idempotent and has a side-effect-free dry-run path.
- CI supports Python 3.12 and 3.13.
- PyPI publication uses trusted publishing from GitHub Releases.
- The repository becomes public only after local verification and secret scanning.
- No force-push to `main`.
- Every production behavior is introduced through a failing test first.

---

### Task 1: Portable schedule model

**Files:**
- Create: `src/touchstone/scheduling/__init__.py`
- Create: `src/touchstone/scheduling/model.py`
- Modify: `src/touchstone/config.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Produces: `parse_schedule(raw: str) -> Schedule`
- Produces: `Schedule.launchd_calendar() -> dict[str, int] | None`
- Produces: `Schedule.systemd_calendar() -> str`

- [ ] **Step 1: Write failing literal schedule tests**

```python
@pytest.mark.parametrize(
    ("raw", "systemd"),
    [("hourly", "hourly"), ("daily@03:15", "*-*-* 03:15:00"), ("weekly@MON,09:30", "Mon *-*-* 09:30:00")],
)
def test_schedule_has_a_stable_systemd_calendar(raw: str, systemd: str) -> None:
    assert parse_schedule(raw).systemd_calendar() == systemd

@pytest.mark.parametrize("raw", ["", "daily@25:00", "weekly@FUNDAY,09:00", "*/5 * * * *"])
def test_invalid_or_unsupported_schedules_are_rejected(raw: str) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(raw)
```

- [ ] **Step 2: Run tests and confirm the schedule model is missing**

Run: `.venv/bin/python -m pytest tests/test_schedule.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement the immutable schedule model and config validation**

```python
@dataclass(frozen=True, slots=True)
class Schedule:
    frequency: Literal["hourly", "daily", "weekly"]
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
```

Use a strict full-match parser and hand-authored weekday map. Keep raw schedule text in config descriptions, normalized fields in runtime state.

- [ ] **Step 4: Run schedule and config tests**

Run: `.venv/bin/python -m pytest tests/test_schedule.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit schedule parsing**

```bash
git add src/touchstone/scheduling src/touchstone/config.py tests/test_schedule.py tests/test_config.py
git commit -m "feat: define portable loop schedules"
```

### Task 2: launchd and systemd scheduler adapters

**Files:**
- Create: `src/touchstone/scheduling/base.py`
- Create: `src/touchstone/scheduling/launchd.py`
- Create: `src/touchstone/scheduling/systemd.py`
- Modify: `src/touchstone/scheduling/__init__.py`
- Modify: `src/touchstone/cli.py`
- Test: `tests/test_scheduling.py`

**Interfaces:**
- Produces: `build_scheduler(platform: str, executor: Executor) -> Scheduler`
- Produces: `install`, `uninstall`, and `status` methods from the design

- [ ] **Step 1: Write failing adapter tests using temporary destinations**

```python
def test_launchd_file_has_absolute_paths_and_no_environment_secrets(tmp_path: Path) -> None:
    report = launchd_scheduler().install(config(), target=tmp_path)
    plist = report.files[0].read_text()
    assert "/absolute/bin/touchstone" in plist
    assert "/absolute/project/touchstone.toml" in plist
    assert "GH_TOKEN" not in plist

def test_systemd_install_is_idempotent(tmp_path: Path) -> None:
    scheduler = systemd_scheduler()
    first = scheduler.install(config(), target=tmp_path)
    second = scheduler.install(config(), target=tmp_path)
    assert first.files == second.files
    assert second.changed == ()
```

- [ ] **Step 2: Run tests and confirm adapters are missing**

Run: `.venv/bin/python -m pytest tests/test_scheduling.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement deterministic rendering and native enable commands**

launchd writes `~/Library/LaunchAgents/io.github.misoto22.touchstone.<loop>.plist`. systemd writes `~/.config/systemd/user/touchstone-<loop>.service` and `.timer`. When `target` is provided, render only and execute no `launchctl` or `systemctl` commands.

- [ ] **Step 4: Add scheduler CLI commands**

```text
touchstone install-scheduler [--dry-run] [--output PATH] [--json]
touchstone uninstall-scheduler [--dry-run] [--json]
touchstone scheduler-status [--json]
```

- [ ] **Step 5: Run adapter, CLI, and foundation tests**

Run: `.venv/bin/python -m pytest tests/test_scheduling.py tests/test_initialize.py tests/test_doctor.py -q`

Expected: PASS.

- [ ] **Step 6: Commit native scheduling**

```bash
git add src/touchstone/scheduling src/touchstone/cli.py tests/test_scheduling.py tests/test_initialize.py tests/test_doctor.py
git commit -m "feat: install native loop schedulers"
```

### Task 3: Public project documentation and policy files

**Files:**
- Create: `LICENSE`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `assets/brand/README.md`
- Test: `tests/test_readme_commands.py`

**Interfaces:**
- Consumes: the installed CLI and generic config from the foundation plan

- [ ] **Step 1: Write a failing README command smoke test**

```python
def test_documented_first_run_commands_exist(installed_touchstone: Path) -> None:
    help_text = run(installed_touchstone, "--help").stdout
    for command in ("init", "doctor", "setup", "run", "status", "install-scheduler"):
        assert command in help_text
```

- [ ] **Step 2: Run the smoke test and confirm missing commands fail**

Run: `.venv/bin/python -m pytest tests/test_readme_commands.py -q`

Expected: FAIL until every documented command exists.

- [ ] **Step 3: Add Apache-2.0 and public contribution/security documents**

`SECURITY.md` directs private vulnerability reports through GitHub Security Advisories. `CONTRIBUTING.md` contains environment setup, TDD, checks, branch naming, and pull-request expectations. `CHANGELOG.md` starts with `0.1.0` and links Keep a Changelog semantics.

- [ ] **Step 4: Rewrite README around the verified first-run path**

README sections, in order: value proposition, safety model, installation, five-minute first run, config reference, lifecycle, commands, scheduling, troubleshooting, security boundary, development, release, license. Use `touchstone-agent` for package installation and `touchstone` for commands.

- [ ] **Step 5: Run README command smoke test and link checks**

Run: `.venv/bin/python -m pytest tests/test_readme_commands.py -q`

Expected: PASS.

- [ ] **Step 6: Commit public documentation**

```bash
git add LICENSE SECURITY.md CONTRIBUTING.md CHANGELOG.md README.md assets/brand/README.md tests/test_readme_commands.py
git commit -m "docs: publish the touchstone operator guide"
```

### Task 4: CI, dependency updates, and trusted release workflow

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`
- Create: `.github/dependabot.yml`
- Create: `.github/ISSUE_TEMPLATE/bug.yml`
- Create: `.github/ISSUE_TEMPLATE/feature.yml`
- Create: `.github/pull_request_template.md`
- Modify: `pyproject.toml`
- Test: `tests/test_distribution.py`

**Interfaces:**
- Produces: verified wheel and source distribution
- Produces: PyPI trusted-publishing job gated by a GitHub Release and `pypi` environment

- [ ] **Step 1: Write a failing isolated-wheel test**

```python
def test_built_wheel_runs_without_the_source_checkout(tmp_path: Path) -> None:
    wheel = build_wheel(tmp_path)
    environment = install_wheel_in_venv(wheel, tmp_path / "venv")
    result = environment.run("touchstone", "graph")
    assert result.returncode == 0
    assert "audit" in result.stdout
```

- [ ] **Step 2: Run the distribution test and confirm missing build tools or package resources fail**

Run: `.venv/bin/python -m pytest tests/test_distribution.py -q`

Expected: FAIL before distribution dependencies and resource inclusion are complete.

- [ ] **Step 3: Add build verification dependencies and workflows**

CI matrix: Python 3.12 and 3.13. Each matrix job installs `.[dev]`, runs pytest, Ruff, and graph check. A Python 3.12 packaging job runs `python -m build`, `twine check dist/*`, and the isolated-wheel smoke test.

Release workflow triggers on `release: published`, downloads no untrusted artifacts, rebuilds from the tag, reruns Twine checks, and uses `pypa/gh-action-pypi-publish` with `id-token: write` in environment `pypi`.

- [ ] **Step 4: Add Dependabot and issue/PR templates**

Dependabot checks pip and GitHub Actions weekly with grouped non-major updates. Templates request reproduction, version, doctor output with secrets removed, and verification evidence.

- [ ] **Step 5: Run full local distribution verification**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m touchstone.cli graph --check && .venv/bin/python -m build && .venv/bin/python -m twine check dist/*`

Expected: all commands exit 0.

- [ ] **Step 6: Commit repository automation**

```bash
git add .github pyproject.toml tests/test_distribution.py
git commit -m "ci: verify and publish touchstone distributions"
```

### Task 5: Public-release verification and GitHub publication

**Files:**
- Modify only when verification exposes a defect

**Interfaces:**
- Produces: public `Misoto22/touchstone` repository and anonymously installable Git wheel

- [ ] **Step 1: Run complete local verification from a clean checkout state**

Run: `git status --short && .venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python -m touchstone.cli graph --check && .venv/bin/python -m build && .venv/bin/python -m twine check dist/*`

Expected: no unexpected worktree changes; all verification commands exit 0.

- [ ] **Step 2: Run secret and personal-value scans**

Run: `gitleaks git . --redact && rg -n '/Users/macbook01|henrycxw@gmail.com|Misoto22/kioku' --glob '!.git/**' .`

Expected: Gitleaks exits 0 and current project files have no owner-specific operational values.

- [ ] **Step 3: Push the implementation branch and open a pull request**

```bash
git push -u origin codex/ready-to-use
gh pr create --repo Misoto22/touchstone --base main --head codex/ready-to-use \
  --title "feat: make touchstone ready to use" --body-file /tmp/touchstone-pr.md
```

Review CI and merge without force-pushing `main`.

- [ ] **Step 4: Change repository visibility and verify anonymous access**

```bash
gh repo edit Misoto22/touchstone --visibility public --accept-visibility-change-consequences
env -u GH_TOKEN -u GITHUB_TOKEN git ls-remote https://github.com/Misoto22/touchstone.git HEAD
```

Expected: visibility is `PUBLIC` and anonymous `ls-remote` returns `HEAD`.

- [ ] **Step 5: Verify installation from the public Git repository**

Use a temporary pipx home and binary directory. Run:

```bash
pipx install git+https://github.com/Misoto22/touchstone.git
touchstone --help
touchstone graph
```

Expected: installation and both commands succeed without the development checkout.

- [ ] **Step 6: Configure the remaining PyPI external prerequisite**

Create the `touchstone-agent` PyPI project through the first trusted-publisher release, with owner `Misoto22`, repository `touchstone`, workflow `release.yml`, environment `pypi`. If PyPI account authorization is unavailable, stop at this external gate and report the exact configuration values; do not use or request a long-lived API token.

- [ ] **Step 7: Create release `v0.1.0` and verify PyPI installation**

After trusted publishing is configured, create the GitHub Release and wait for the release workflow. Then install into a clean temporary pipx home:

```bash
pipx install touchstone-agent==0.1.0
touchstone --help
```

Expected: PyPI installation succeeds and exposes the `touchstone` command.
