# Stack Profiles and GitHub Actions Design

## Status

Approved on 2026-08-24 after the `grill-with-docs` design tree reached an empty frontier.
This specification supersedes the configuration, scheduling, status, and hosted-execution
sections of `2026-08-24-ready-to-use-design.md`. The root `CONTEXT.md` defines canonical
language; `docs/adr/0001-*.md` through `docs/adr/0021-*.md` record the trade-offs behind it.

## Goal

Touchstone adopts an authorized repository, identifies its technology stack, generates a
reviewable audit configuration, and runs the same safe loop locally, over SSH, or from GitHub
Actions. A newcomer can install the Python package, initialize the repository, inspect the
generated decisions, verify prerequisites, and enable a scheduled pull-request workflow
without editing Python or storing secrets in TOML.

## Supported Profiles

The first built-in catalog is:

- `generic` — project-neutral audit with no framework validation assumptions;
- `javascript` — JavaScript package ecosystem or browser toolchain;
- `node` — confirmed Node.js runtime;
- `typescript` — TypeScript project configuration;
- `react` — direct React dependency;
- `nextjs` — confirmed Next.js application evidence;
- `python` — Python project metadata plus source evidence;
- `fastapi` — confirmed FastAPI application evidence;
- `django` — confirmed Django application evidence.

Profiles are composable and independent of Execution Backends. Detection uses repository-owned
manifests, lockfiles, dependency declarations, workspace declarations, and framework
configuration. It never imports project configuration, executes project code, calls a model, or
uses locally installed tools as evidence.

Detection returns `confirmed`, `candidate`, or `unsupported` with a list of evidence. High-
confidence results are materialized automatically. Interactive initialization asks only about
candidates and conflicting package-manager evidence. Non-interactive initialization fails with
an actionable candidate report unless explicit `--profile` or package-manager choices resolve
the ambiguity.

## Project Targets

A repository contains one or more Project Targets with stable logical IDs and repository-relative
paths. The repository root and members declared by npm, pnpm, Yarn, uv, or supported PDM
workspace metadata are eligible for automatic Target creation. Nested manifests outside an
explicit workspace are candidates, not automatic Targets. Submodules, dependency directories,
virtual environments, generated trees, fixtures, examples, and vendor directories are excluded.

A pure workspace root is a Workspace Container. It becomes a Target only when it has its own
source or independent validation contract. Changed Targets expand through the declared reverse
workspace dependency graph. File ownership chooses the deepest matching Target path.

## Configuration v2

The project-owned `touchstone.toml` has `version = 2` and references
`.touchstone/generated.toml`. The generated file records generator/package version, Profile
versions, evidence, Target IDs and paths, candidate/confirmed verdicts, validation candidates,
and Profile-derived path/context defaults. It contains no secret.

The loader reads generated configuration first and applies project-owned root configuration with
semantic precedence:

1. explicit project configuration;
2. repository-local declarative Profiles;
3. built-in framework Profiles;
4. built-in language/runtime Profiles.

Lists merge without duplicates. Scalars that cannot be resolved by this precedence fail with a
field-qualified configuration error. Local Profiles live under `.touchstone/profiles/`, use TOML
and Markdown only, and cannot import Python, run shell hooks, or download remote resources.

Profile refresh replaces `.touchstone/generated.toml`, preserves `touchstone.toml`, and displays
a diff before writing. `--check` reports drift without writing. A v1 configuration remains
readable. Migration to v2 previews both files, backs up v1, and requires explicit confirmation
when schedule anchor or timezone semantics change.

The v2 schedule contract is:

- `hourly@MM`;
- `daily@HH:MM`;
- `weekly@DAY,HH:MM` with `MON` through `SUN`;
- omitted for manual-only Loops.

The repository has one explicit IANA timezone, defaulting to `UTC` for new projects. New
Repository Loops default to `hourly@00`.

## Validation

Profiles may supply detection rules, audit/review guidance, conventional source/protected paths,
Preparation requirements, and Validation Candidates. Profiles never choose the engine, model,
budget, schedule, GitHub permissions, secrets, or auto-merge policy.

A Validation Gate is an enabled structured command containing an argv tuple, Target working
directory, timeout, preparation requirement, and capability declaration. Shell strings are
rejected unless an explicit shell executable and risk acknowledgement are present. Failure or
timeout blocks publication and cannot be waived by the authoring agent.

Only side-effect-minimal checks are enabled automatically. Package scripts, pytest, Next.js
build/type generation, Django checks, database/service tests, installers with lifecycle scripts,
and commands that load application settings are materialized disabled until the project opts in.
Tracked-file changes during validation fail the Gate.

The Preparation Stage runs before model or publishing credentials exist. It uses a confirmed
lockfile/frozen package-manager operation. Node lifecycle scripts and Python build hooks require
explicit enablement. Validation receives no model key, App key, project secret, or external-
service credential.

## Run and Change State

Run Outcome and Change Lifecycle are separate stable machine contracts.

Run Outcomes:

- `completed` — the requested operation completed; lifecycle tells what now waits;
- `no_change` — no eligible repository change was produced;
- `blocked` — a deterministic safety, validation, permission, health, or setup prerequisite failed;
- `failed` — an engine, contract, tool, transport, or publication operation failed;
- `rehearsed` — a dry run completed without publication.

Change Lifecycles:

- `proposed`, `awaiting_human`, `awaiting_checks`, `merged`, `closed`, `reaped`, `failed`.

Exit codes are `0` for completed/no-change/rehearsed, `3` for blocked, `1` for failed, `2` for
CLI usage, and `78` for invalid configuration. `status` is read-only. `reconcile` is the explicit
operation that compares ledger state to GitHub and advances lifecycle.

Resume accepts only `approve`, `close`, or `reanalyze`, bound to a specific Loop, candidate, and
Snapshot Lineage. Partial external writes are recorded with `partial = true`, retain all remote
identifiers, and must reconcile before continuation.

## Durable Scheduling

launchd, systemd, and GitHub Actions provide Wake Signals to the same `run-due` evaluator. A
Due Slot is `(loop_id, schedule_generation, scheduled_for_utc)`. Before model work, the evaluator
acquires a durable expiring claim. Completed, no-change, rehearsed, and blocked outcomes consume
the slot after a durable snapshot. Failed slots retry with centralized backoff for at most three
total attempts by default, then record terminal failure and advance. Expired claims can resume.

Missed periods coalesce into one current-state Catch-up Run with `missed_count` and lateness. A
Clean Start runs one immediate baseline. Schedule changes create a new generation and never replay
the old generation. Multiple due Loops run by explicit priority and then Loop ID. A no-change
result continues; an active proposed change or repository-wide blocked/failed result leaves the
remaining Loops due.

The generated hosted workflow defaults to an off-hour 15-minute Wake Signal for public
repositories and 60 minutes for private repositories, both configurable. Manual dispatch runs
the default-branch due path, may select one Loop, and requires explicit `force` to ignore due time;
force never bypasses safety Gates.

## GitHub Actions

`touchstone actions init` writes a reviewable repository workflow. The workflow owns triggers,
jobs, permissions, concurrency, secret mapping, and immutable Action SHAs. First-party composite
Actions encapsulate step mechanics only and cannot elevate permissions.

The workflow accepts only `schedule` and `workflow_dispatch` on the default branch. Mutating runs
use one repository concurrency group with `cancel-in-progress: false` and coalesce pending Wake
Signals. The default is Unattended PR Mode: the workflow may open a PR but never enables auto-
merge unless project configuration explicitly opts in. Approval-Gated Mode optionally protects
the Publish Stage with a GitHub Environment.

The Preparation Stage has no secrets. The Analysis Stage has the selected provider API key but no
write token. The Publish Stage mints a short-lived installation token from the repository owner's
GitHub App and has no model key. Repository or organization secrets are the portable baseline;
Environments are optional. Commits use the installed App bot identity.

State and candidate bundles are encrypted client-side with a dedicated 32-byte repository secret.
Only schema, run identity, compatibility metadata, and ciphertext digest remain plaintext.
Snapshots are immutable, attached to a specific workflow run, retained for 90 days by default,
and restored only after matching repository, Loop, config/Profile digest, lineage, and payload
digest. Missing or incompatible state records a Clean Start.

## Actions Setup

`touchstone actions init` changes repository files only and always shows a diff. `touchstone
actions setup` is interactive and resumable. It uses the GitHub App Manifest flow, a state-checked
loopback callback with a manual code fallback, and two explicit browser confirmations: create the
owner-controlled App, then install it on the target repository.

The one-time PEM stays in process memory while the CLI writes it to GitHub secrets through stdin.
It is never written to the repository or a normal local file. If interrupted after App creation
but before secret storage, doctor reports Partial Setup and directs the owner to generate a
replacement key in GitHub. Setup discovers and reuses completed steps.

## Public CLI Surface

The implementation adds or changes these commands while retaining compatible v1 commands:

```text
touchstone init [--profile NAME] [--package-manager NAME]
touchstone profile detect|diff|refresh
touchstone config migrate [--check] [--write]
touchstone validate [LOOP] [--target ID]
touchstone run LOOP [--dry-run]
touchstone run-due [--loop ID] [--force]
touchstone status [--json]
touchstone reconcile [--json]
touchstone resume CANDIDATE approve|close|reanalyze
touchstone actions init [--check] [--action-sha SHA]
touchstone actions setup [--check]
touchstone doctor [--actions] [--json]
```

Human output explains evidence and repair actions. JSON uses versioned field names and never
contains credentials or decrypted snapshots.

## README Contract

README follows the installed `docs:readme` canonical shape and repository English. Its first
pasteable path is:

```bash
pipx install touchstone-agent
cd /path/to/authorized/repository
touchstone init
touchstone doctor
touchstone run code --dry-run
```

GitHub Actions onboarding is a second explicit path using `touchstone actions init`, review of the
workflow diff, `touchstone actions setup`, and `touchstone doctor --actions`. README states the
browser confirmations, owner-App boundary, secret names, public-artifact encryption, default
PR-only policy, schedule latency, public-repository inactivity behavior, and v1 migration path.
Every documented command is verified against installed-wheel CLI help.

## Verification Gates

Completion requires:

- schema v1 compatibility and v2 migration round-trip tests;
- Profile detection fixtures for every built-in and mixed monorepos;
- no detector executes repository code or follows excluded paths;
- generated/override refresh and drift tests;
- Validation Gate side-effect and secret-scrubbing tests;
- canonical outcome, exit-code, pure-status, reconcile, resume, and partial-failure tests;
- Due Slot claim, expiry, catch-up, retry, DST, and backend-parity tests;
- encrypted snapshot round-trip, tamper, incompatibility, and path-traversal tests;
- generated workflow permissions, triggers, concurrency, immutable-SHA, and secret-scope tests;
- mocked App manifest/setup interruption and recovery tests;
- full pytest, Ruff, graph check, package build, Twine check, isolated-wheel smoke test;
- README command smoke tests and rendered GitHub verification on the implementation branch.
