# Touchstone Ready-to-Use Design

## Status

Approved direction: a self-hosted CLI distributed as the `touchstone-agent`
Python package, with the `touchstone` command as its stable user interface.

License: Apache-2.0.

## Goal

A new user can install Touchstone with `pipx`, initialize it against an
authorized GitHub repository, validate the installation, perform a safe dry
run, and install a native scheduler without editing Python or copying a
project-specific example.

The complete first-run path is:

```text
pipx install touchstone-agent
touchstone init
touchstone doctor
touchstone setup
touchstone run code --dry-run
touchstone install-scheduler
```

## Product Boundaries

Touchstone owns:

- configuration discovery and validation;
- model execution through Codex CLI or Claude CLI;
- isolated worktrees, checkpoints, and the internal event ledger;
- deterministic diff and publication policy;
- GitHub pull-request creation, reconciliation, reaping, and resume safety;
- native per-user scheduling on macOS and Linux;
- diagnostics, status output, and structured run records.

The target repository owns:

- its application and test commands;
- branch protection and required checks;
- deployment and production verification workflows;
- credentials and engine authentication;
- project-specific audit rules and custom briefs.

Touchstone may inspect these repository-owned surfaces and refuse to run when
their configured guarantees are unavailable. `touchstone setup` does not
weaken or silently create branch protection.

## Non-Goals

- A hosted SaaS, dashboard, or long-running web daemon.
- General issue triage or feature implementation.
- Automatic configuration of credentials.
- Support for GitLab or Bitbucket in the first public release.
- A universal cron expression translator.
- Automatic publication to PyPI without a configured GitHub trusted publisher.

## Architecture

The existing LangGraph remains responsible for the audit, classification,
independent review, publication, and human-resume branches. Operational
concerns move behind three deep modules.

### Configuration module

Interface:

```python
load_config(path: Path | None = None) -> LoadedConfig
discover_project(start: Path) -> ProjectDiscovery
initialize_config(options: InitOptions) -> Path
```

The implementation owns search precedence, path resolution, environment
overrides, schema validation, built-in brief resolution, Git discovery, and
safe serialization. Callers receive one normalized `Config` and do not need to
know where individual values came from.

### Repository lifecycle module

Interface:

```python
reconcile(loop: LoopConfig) -> ReconcileReport
publish(request: PublicationRequest) -> PublicationResult
resume(thread_id: str, decision: Decision) -> PublicationResult
```

The implementation owns GitHub state lookup, pull-request state transitions,
head-SHA validation, auto-merge arming, ready/draft transitions, stale pull
reaping, ledger projection, and idempotency. The graph asks for an outcome; it
does not orchestrate individual `gh` calls.

### Scheduler module

Interface:

```python
install(config: Config, target: Path | None = None) -> InstallReport
uninstall(config: Config, target: Path | None = None) -> InstallReport
status(config: Config) -> SchedulerStatus
```

Two adapters make this a real seam:

- launchd user agents on macOS;
- systemd user services and timers on Linux.

Both adapters consume the same normalized schedule and invoke the same CLI
command. Installation is idempotent and supports a dry-run destination for
tests and operator review.

## Configuration Contract

The TOML schema starts with `version = 1`. Unknown keys are errors so a typo
cannot silently disable a safety setting.

Configuration search order, highest priority first:

1. `--config PATH`;
2. `TOUCHSTONE_CONFIG`;
3. `touchstone.toml` found from the current directory upward to the Git root;
4. `$XDG_CONFIG_HOME/touchstone/config.toml`;
5. `~/.config/touchstone/config.toml`.

Relative paths are resolved relative to the configuration file, never the
process working directory. Secrets are forbidden in TOML; engine and GitHub
credentials remain in their native CLI stores or environment.

Example shape:

```toml
version = 1

[project]
path = "."

[forge]
provider = "github"
required_workflows = ["ci.yml"]
reap_after_hours = 6

[engine]
name = "codex"
model = "gpt-5.6-sol"
audit_effort = "high"
review_effort = "high"
timeout_seconds = 2700

[execution]
target = "local"

[git]
# Omit both values to inherit the target repository's Git configuration.
author_name = "Touchstone"
author_email = "touchstone@users.noreply.github.com"

[loop.code]
brief = "builtin:code-audit"
label = "touchstone:audit"
schedule = "hourly"
protected_paths = [".github/", "migrations/"]

[loop.code.context]
project = "this repository"
ledger = "the configured project findings ledger"
protected = "the configured protected paths"
rules_clause = ""
```

`forge.slug` and `forge.default_branch` are optional. Discovery derives them
from `origin` and GitHub. Explicit values are supported for mirrors and unusual
remote layouts.

The schedule vocabulary is intentionally portable:

- `hourly`;
- `daily@HH:MM`;
- `weekly@DAY,HH:MM`.

Times use the host's local timezone. `touchstone doctor` reports the detected
IANA timezone and warns when the scheduler cannot establish it. Each loop owns
its own schedule.

Built-in briefs are package resources referenced as `builtin:<name>`. Custom
brief paths are relative to the configuration file. The installed wheel and
source checkout therefore behave identically.

## First-Run Commands

### `touchstone init`

Interactive by default. It discovers the Git repository, `origin`, default
branch, installed engines, and host scheduler. It asks only for decisions that
cannot be discovered: engine, model, loop selection, required workflow names,
and schedule.

It refuses to overwrite an existing config unless `--force` is supplied.
`--non-interactive` supports automation and requires every non-discoverable
value as an explicit flag.

### `touchstone doctor`

Read-only. Every check returns `PASS`, `WARN`, or `FAIL` with a stable check ID,
plain-language explanation, and a concrete repair command where one exists.

Checks include:

- config schema and resolved paths;
- Git repository and clean remote discovery;
- `gh` availability and access to the configured repository;
- Codex or Claude CLI availability;
- configured model and engine capability warnings;
- required workflows and their latest default-branch conclusions;
- GitHub auto-merge and branch-protection visibility;
- labels, state directory, SQLite, and worktree support;
- scheduler support and installed status.

Human output is the default; `--json` emits the structured report and never
includes credentials.

### `touchstone setup`

Idempotently creates the state directory and configured GitHub labels. It
prints every planned mutation and supports `--dry-run`. It does not modify
branch protection, repository secrets, Actions permissions, or engine login.

### `touchstone status`

Runs reconciliation, then reports current loop slots, parked decisions,
pending auto-merges, failed or reaped pull requests, last run outcomes, and
scheduler status. `--json` uses the same structured model as the human view.

### `touchstone install-scheduler`

Writes and enables one native user timer per scheduled loop. Generated jobs use
the absolute executable path, absolute config path, explicit working directory,
and a constrained `PATH`. They do not embed credentials.

The command prints installed paths and the corresponding native inspection and
uninstall commands. `--dry-run` renders files without enabling them.

## Pull-Request Lifecycle

The internal ledger becomes an append-only event stream. Each finding has a
stable identifier derived from loop name and normalized title, plus the branch,
pull-request number, reviewed head SHA, timestamps, and transition detail.

States:

```text
proposed
  ├─ low + approved ─▶ armed ─▶ merged
  │                         ├─▶ failed
  │                         └─▶ reaped
  └─ medium/high/rejected ─▶ parked
                               ├─ human merge ─▶ armed
                               └─ human close ─▶ closed
```

Only `armed`, `parked`, and `merged` suppress rediscovery. `failed`, `reaped`,
and `closed` remain visible history but do not permanently hide a defect.

Reconciliation runs before gates, from `touchstone status`, and before resume.
It compares ledger projections with the live pull request and records terminal
state changes. A stale non-draft automated pull is reaped only when it is older
than `reap_after_hours` and its checks have conclusively failed. Drafts waiting
for people are never automatically closed.

Auto-merge is successful only when GitHub accepts the request. Failure returns
a held result and is never recorded as `armed`.

Resume acquires the same run lock, loads the checkpoint, reconciles the live
pull request, and verifies all of the following:

- the pull request still exists and is open;
- its head SHA equals the SHA that was independently reviewed;
- configured production/default-branch health gates still pass;
- the pull request can be marked ready;
- GitHub accepts auto-merge.

Any mismatch holds the run and explains the required next action. A changed
head must be independently reviewed again; a human answer cannot approve an
unreviewed commit.

## Audit and Review Contracts

Both finding and review outputs use explicit JSON schemas. Missing, malformed,
or unrecognized output is `inconclusive`, never `clean`.

Risk can only increase. Built-in deterministic escalation covers:

- configured protected paths;
- GitHub workflows and repository policy files;
- credential and environment files;
- schema and migration paths;
- translated-document twins;
- changes outside a loop's configured remit.

Project configuration may add paths but cannot remove the built-in protection
for Touchstone's own control files without an explicit unsafe override. Unsafe
overrides are visible in `doctor` and disable unattended auto-merge.

The author session never receives GitHub or package-publishing credentials from
Touchstone. Claude retains explicit command denials. Codex retains its declared
whole-worktree limitation; deterministic diff classification remains the final
write-policy enforcement.

## Observability

Every run gets a stable run ID. Human CLI output stays concise while a JSONL
event log records:

- normalized configuration fingerprint;
- loop, engine, model, effort, executor, and host;
- gate and reconciliation results;
- session duration, timeout status, and reported cost when available;
- finding ID, risk transitions, review verdict, PR, and final outcome.

No prompt body, credentials, environment values, or complete command line is
recorded. `touchstone status --json` exposes projections, not raw secrets or
model transcripts.

## Packaging and Repository Standards

- Distribution name: `touchstone-agent`.
- Command: `touchstone`.
- Python: 3.12 and 3.13 in CI.
- Build backend: Hatchling.
- License: Apache-2.0.
- Built-in briefs live inside `src/touchstone` and ship in wheels.
- The committed example config is generic and generated from the same renderer
  used by `touchstone init`.
- LangGraph runtime state is ignored and removed from version control.
- Personal filesystem paths, names, email addresses, repositories, workflows,
  and models do not appear as operational constants.

Repository files added for the public release:

- `LICENSE`;
- `SECURITY.md`;
- `CONTRIBUTING.md`;
- `.github/workflows/ci.yml`;
- `.github/workflows/release.yml` using PyPI trusted publishing;
- `.github/dependabot.yml`;
- generic issue templates;
- updated README with installation, first run, config reference, lifecycle,
  troubleshooting, security boundaries, and development instructions.

The release workflow builds and verifies distributions before publishing. It
only publishes from a GitHub Release and requires the repository's `pypi`
environment to be connected to the `touchstone-agent` PyPI trusted publisher.

## Testing Strategy

All behavior changes follow red-green-refactor.

Fast tests exercise configuration discovery, strict validation, built-in brief
loading, schedule parsing, doctor results, ledger projection, and lifecycle
transitions through public interfaces.

Integration tests execute:

- `touchstone init --non-interactive` against a temporary Git repository;
- wheel build, installation into an isolated environment, and CLI invocation;
- launchd and systemd rendering into temporary directories;
- the real local Git adapter with temporary bare remotes;
- a complete publication lifecycle through a stateful fake `gh` executable
  that mirrors documented GitHub JSON shapes and failure modes.

The fake replaces only the external GitHub network. Tests assert Touchstone's
observable state, files, exit codes, and ledger events rather than calls made
to the fake.

CI gates:

```text
pytest
ruff check .
touchstone graph --check
python -m build
twine check dist/*
isolated wheel smoke test
```

## Migration and Compatibility

- Existing unversioned TOML is loaded as version 0 and receives a precise
  migration error with `touchstone config migrate` instructions.
- `touchstone config migrate` writes a sibling backup before replacing a file.
- Existing JSONL ledger rows remain readable and are projected into the new
  event model without rewriting history.
- Existing `run`, `resume`, and `graph` commands remain available.
- `TOUCHSTONE_ENGINE`, `TOUCHSTONE_MODEL`, `TOUCHSTONE_EFFORT`,
  `TOUCHSTONE_REVIEW_EFFORT`, `TOUCHSTONE_TIMEOUT`, `TOUCHSTONE_TARGET`,
  `TOUCHSTONE_REPO`, and `TOUCHSTONE_STATE` remain supported for one minor
  release and produce a deprecation warning in `doctor`.

## Public-Release Sequence

1. Implement and verify the complete local first-run flow.
2. Remove generated LangGraph state and all current hardcoded personal values.
3. Build and smoke-test the wheel in a clean environment.
4. Push the implementation branch and merge through a reviewed pull request.
5. Change `Misoto22/touchstone` visibility from private to public.
6. Verify anonymous repository access and installation from the public Git URL.
7. Configure PyPI trusted publishing and create the first GitHub Release.
8. Verify `pipx install touchstone-agent` from PyPI in a clean environment.

The existing Git history is preserved. No force-push to `main` is permitted.
Historical author metadata remains part of Git history; current project files
contain no machine- or owner-specific operational values.

## Acceptance Criteria

- A clean macOS or Linux account can follow the README without editing source.
- `touchstone init` produces a valid generic config without owner-specific
  values.
- `touchstone doctor` identifies every missing prerequisite before a paid model
  session starts.
- `touchstone setup --dry-run` and scheduler dry runs have no external effects.
- No failed auto-merge is reported as merging.
- Closed, failed, or reaped pull requests cannot permanently hide findings.
- Resume cannot merge a head SHA that the independent reviewer did not review.
- Invalid model output is inconclusive rather than clean.
- Native scheduler files contain absolute executable/config paths and no
  credentials.
- Source checkout and installed wheel resolve the same built-in briefs.
- CI verifies tests, lint, graph freshness, distributions, and wheel execution.
- The GitHub repository is publicly readable under Apache-2.0.
- The published PyPI distribution installs with `pipx install touchstone-agent`
  and exposes the `touchstone` command.
