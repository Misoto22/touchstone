# Touchstone

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/touchstone-readme-hero-dark.png">
  <img src="assets/brand/touchstone-readme-hero.png" alt="Touchstone repository audit lifecycle" width="900">
</picture>

<br />

**Stack-aware repository audit loops, locally or in GitHub Actions**

Touchstone turns agent findings into reviewable, PR-only changes.

<br />

[PyPI](https://pypi.org/project/touchstone-agent/) · [Install](#getting-started) · [GitHub Actions](#github-actions) · [Architecture](https://github.com/Misoto22/touchstone/tree/main/docs/adr/) · [Report issue](https://github.com/Misoto22/touchstone/issues)

<br />

[![Python 3.12+](https://img.shields.io/badge/Python_3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/touchstone-agent)](https://pypi.org/project/touchstone-agent/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/Misoto22/touchstone/blob/main/LICENSE)

</div>

---

### Features

- **Stack detection before configuration** — identifies stable package-backed Targets and composes `generic`, `javascript`, `node`, `typescript`, `react`, `nextjs`, `python`, `fastapi`, and `django` Profiles from repository evidence.
- **Monorepo-aware scope** — discovers npm, pnpm, Yarn, Bun, uv, Poetry, and PDM evidence, records a package manager per Target, tracks dependency edges, and validates a changed Target plus its dependents.
- **Owned configuration** — keeps deliberate settings in `touchstone.toml` and reproducible stack evidence in `.touchstone/generated.toml`; Profile refresh never replaces project overrides.
- **Structured validation** — runs argv-based gates in bounded Target directories with timeouts, scrubbed subprocess environments, hook-free locked preparation, and tracked-file mutation checks. Generated commands use the Target's own package manager. A Profile enables only side-effect-minimal Gates; every command that runs project code stays a disabled Candidate until the project override accepts it.
- **Two execution backends** — runs from native launchd/systemd wake signals or a generated, repository-owned GitHub Actions workflow.
- **Split hosted trust stages** — separate Prepare, Analysis, Verify, mutation-only Publish, and Snapshot jobs give each stage one credential domain, and mint the repository-scoped App token only after an independent stage has validated the candidate.
- **Reproducible hosted runtime** — one credential-free Action step installs hash-locked Python dependencies, the exact Agent CLI named by the committed `npm` lockfile, and the project's own locked dependencies, then attests that environment to the repository HEAD, configuration, Targets, and lockfiles.
- **PR-only lifecycle** — low-risk approved candidates open ready pull requests; higher-risk or rejected candidates open drafts. Auto-merge remains disabled.
- **Durable recovery** — transactional Due Slot claims, encrypted state snapshots, stable run outcomes, explicit change states, and exact-candidate resume decisions survive retries, missed wake signals, and a Publish job that dies mid-publication.

---

### Tech Stack

<table>
<tr><td><b>Runtime</b></td><td>Python 3.12+ · LangGraph · SQLite</td></tr>
<tr><td><b>Agents</b></td><td>Codex CLI · Claude Code CLI</td></tr>
<tr><td><b>Stack model</b></td><td>Declarative TOML Profiles · Targets</td></tr>
<tr><td><b>Local backend</b></td><td>launchd on macOS · systemd user timers on Linux</td></tr>
<tr><td><b>Hosted backend</b></td><td>GitHub Actions · owner-controlled GitHub App · AES-256-GCM artifacts</td></tr>
<tr><td><b>Quality</b></td><td>pytest · Ruff · Hatchling · isolated-wheel acceptance tests</td></tr>
</table>

---

### Project Structure

```text
src/touchstone/
├── profiles/               Safe detection, workspace discovery, and materialization
├── resources/profiles/     Nine built-in declarative stack Profiles
├── hosted/                 Workflow, crypto, runtime stages, and GitHub App setup
├── scheduling/             Portable schedules, durable Due Slots, launchd, systemd
├── nodes/                  Audit, classify, independent review, and publication
├── engines/                Codex and Claude execution contracts
├── execution/              Local and SSH command runners
├── validation.py           Structured preparation and Validation Gates
├── lifecycle.py            PR publication, reconciliation, reaping, and resume
├── config.py               Versioned, project-neutral configuration
└── cli.py                  Installed command surface
action.yml                  Pinned composite Action with five explicit stages
action-requirements.lock    Hash-locked Python runtime for the composite Action
agent-runtime/              Integrity-locked Codex and Claude CLI runtimes
docs/adr/                   Architecture decisions
tests/fixtures/acceptance/  Next.js, Django, and mixed-monorepo wheel fixtures
```

---

### Getting Started

Install Touchstone, then rehearse one loop inside a GitHub repository you are authorised to audit:

```bash
pipx install touchstone-agent
cd /path/to/your/repository
touchstone init
touchstone profile detect
touchstone doctor
touchstone setup --dry-run
touchstone setup
touchstone doctor
touchstone run code --dry-run
```

`touchstone init` finds the Git root, GitHub slug, default branch, package managers, workspace Targets, and stack Profiles. A Target ID comes from the package name in `package.json` or `pyproject.toml`, so it survives renaming the checkout directory; a repository-relative path hash covers collisions and unnamed Targets. It asks for the engine, model, required default-branch workflow, Loop schedule, repository visibility, and hosted wake cadence, then writes the project-owned and generated configuration files. The rehearsal runs the configured model and validation path but does not publish.

**Prerequisites** — Python 3.12+, pipx, Git, authenticated `gh`, and an authenticated Codex or Claude CLI. Native scheduling is supported on macOS and Linux.

For non-interactive initialization, provide every decision explicitly:

```bash
touchstone init --non-interactive \
  --engine codex \
  --model YOUR_MODEL_ID \
  --workflow ci.yml \
  --schedule hourly@00 \
  --timezone Australia/Sydney \
  --visibility public \
  --wake-minutes 15
```

Use `--visibility private` for a private repository. Hosted wake cadence accepts `5`, `10`, `15`, `20`, `30`, or `60` minutes; defaults are 15 minutes for public repositories and 60 minutes for private repositories.

---

### Documentation

- [Operator and design context](https://github.com/Misoto22/touchstone/blob/main/CONTEXT.md)
- [Generated loop graph](https://github.com/Misoto22/touchstone/blob/main/docs/graph.md)
- [Architecture Decision Records](https://github.com/Misoto22/touchstone/tree/main/docs/adr/)
- [Stack Profiles and Actions design](https://github.com/Misoto22/touchstone/blob/main/docs/superpowers/specs/2026-08-24-stack-profiles-actions-design.md)
- [Example project configuration](https://github.com/Misoto22/touchstone/blob/main/touchstone.example.toml)
- [Example generated configuration](https://github.com/Misoto22/touchstone/blob/main/touchstone.generated.example.toml)
- [Security policy](https://github.com/Misoto22/touchstone/blob/main/SECURITY.md)

---

### GitHub Actions

The hosted backend is generated into the repository being audited. The repository owns its triggers, permissions, concurrency, secret mappings, retention, and immutable Action references.

```bash
touchstone actions init
touchstone actions init --check
git add .github/workflows/touchstone.yml touchstone.toml .touchstone/generated.toml
git commit -m "ci: add touchstone audit loop"
```

`actions init` resolves the release tag matching the installed `touchstone-agent` version — `v0.1.2` today — to a 40-character commit SHA, so the generated workflow pins the revision this CLI documents rather than a moving branch. Automation may pass an audited revision with `--action-sha`, and must when a development build is installed. `--check` is read-only and exits `3` when the committed workflow has drifted.

> [!WARNING]
> `touchstone actions setup` opens GitHub twice: first to review and create an owner-controlled GitHub App, then to install it for only the selected repository. Read the owner, repository, repository scope, and permissions on both GitHub pages before confirming. The one-time private key is piped directly to `gh secret set`; Touchstone never writes it to disk.

Run the guided setup from a trusted local terminal:

```bash
touchstone actions setup
gh secret set OPENAI_API_KEY --app actions
touchstone actions setup --check
touchstone doctor
```

For Claude, set `ANTHROPIC_API_KEY` instead. Organization-owned repositories use `touchstone actions setup --organization`, which creates the App under the organization and stores its secrets as organization secrets restricted to the selected repository. If loopback callback delivery is unavailable, use `touchstone actions setup --manual-code` and paste the one-time manifest code at the hidden prompt. Setup uses the one-time App private key in memory to verify the live installation, selected-repository scope, and permissions before storing the key as an Actions secret, then zeroes its memory buffer. It records only a non-secret attestation and gives an explicit replacement-key command when the one-time key can no longer be recovered.

Later `touchstone actions setup --check` and `touchstone doctor` can inspect only that cached attestation because Touchstone deliberately does not retain the private key. Doctor therefore reports a warning, not a live pass; a successful hosted Publish token-mint is the current end-to-end proof that the App installation still works.

The standard repository secrets are:

| Secret | Available to |
|---|---|
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | Analysis only |
| `TOUCHSTONE_APP_ID` and `TOUCHSTONE_APP_PRIVATE_KEY` | Publish only |
| `TOUCHSTONE_STATE_KEY` | Analysis, Verify, Publish, and Snapshot |

The workflow has only `schedule` and `workflow_dispatch` triggers. It does not run on pull requests. Public repositories default to off-hour 15-minute wake signals; private repositories default to one off-hour wake per hour. Configure a supported interval with `actions.wake_minutes`, then rerun `touchstone actions init`. Each wake evaluates durable schedules, so frequent wake signals do not imply frequent model calls.

Each job runs the composite Action's credential-free install step first. That step maps no secret at all: it installs the hash-locked Python runtime, the locked Agent CLI, and the project's locked dependencies, then writes a non-secret attestation binding them to the repository HEAD, configuration digest, Target set, and lockfiles. A later stage reuses that exact environment; a mismatched or absent attestation fails closed unless the process still holds no model credential, so dependencies are only ever installed before model credentials exist.

| Stage | Model credential | Publishing credential | Repository token | State key |
|---|---|---|---|---|
| Prepare | no | no | read | no |
| Analysis | yes | no | no | yes |
| Verify | no | no | read | yes |
| Publish | no | App token | App token | yes |
| Snapshot | no | no | no | yes |

Prepare restores the latest encrypted state envelope without a decryption key. Analysis decrypts state, claims one Due Slot, runs the model, and emits an authenticated, candidate-named artifact whose unique ID binds the stable finding, base SHA, patch digest, and run. Verify runs on a separate runner that holds no model credential and no publishing credential; it does receive a repository read token and the state-decryption key. It checks repository, effective non-secret configuration, Profile digest, independently exported Loop and candidate lineage, base SHA, patch digest, health gates, and Validation Gates in a disposable worktree, then emits a versioned attestation. Publish starts on another clean runner, reconstructs the exact candidate from its authenticated artifact, checks the attestation, mints a short-lived App token scoped to the current repository, and performs only publication. Hosted commits are authored by the publishing App's own bot identity rather than the runner's synthesized one. Snapshot has neither model nor publishing credentials. It finalizes the Due Slot, retains state under the full configuration digest for 90 days by default, and — when Publish failed or was cancelled without recording an outcome — reconstructs a `failed` partial marker from the authenticated candidate so the next run is blocked until `touchstone reconcile` inspects that exact branch.

Locked preparation and Validation Gates run as subprocesses with a scrubbed environment — an allowlist of locale, path, and cache variables, a throwaway `HOME`, and no inherited credential — so project code never sees the state key or any token. Model processes get a separate allowlist carrying only that engine's own credential. Health checks are the deliberate exception: they call `gh` and therefore do use the repository read token, which is why Verify holds one.

Hosted operator decisions use the exact candidate ID:

```bash
gh workflow run touchstone.yml -f candidate_id=CANDIDATE_ID -f decision=approve
gh workflow run touchstone.yml -f candidate_id=CANDIDATE_ID -f decision=close
gh workflow run touchstone.yml -f candidate_id=CANDIDATE_ID -f decision=reanalyze
```

GitHub disables scheduled workflows after 60 days without activity in a public repository. `touchstone doctor` reports a disabled workflow and warns once the latest push is 45 days old; `workflow_dispatch` remains the explicit recovery path.

---

### Configuration and Profiles

`touchstone init` writes schema v2. An existing schema-v1 configuration keeps loading unchanged — including one at `~/.config/touchstone/config.toml` — and upgrading is an explicit command, never something a Touchstone upgrade performs on its own. A v1 deployment that is already running does not need to migrate to keep working.

Schema v2 splits ownership across two files:

- `touchstone.toml` is project-owned. It holds repository identity, engine, schedule, Actions policy, Loop choices, and explicit overrides.
- `.touchstone/generated.toml` is machine-owned. It records package/Profile versions, source digest, per-Target package managers, Targets, evidence, dependencies, protected/source paths, and validation candidates.
- `.touchstone/profiles/*.toml` may add repository-local declarative Profiles. Profile files cannot import or execute project code.

Detection distinguishes confirmed evidence, candidates requiring explicit confirmation, and unsupported version ranges. Floating or unresolvable framework versions remain candidates instead of being treated as confirmed. Repository-local declarative Profile detectors participate in the same bounded detection pass. In non-interactive mode, unresolved candidates or ambiguous lockfile families stop initialization instead of guessing. Select deliberately with `--profile NAME` or `--package-manager NAME`.

```bash
touchstone profile detect --json
touchstone profile diff
touchstone profile refresh --check
touchstone profile refresh --write
touchstone validate code
```

`profile refresh --check` and `profile diff` are read-only and exit `3` on drift. `--write` replaces only generated configuration, removes stale auto-detected Profiles, retains Profiles explicitly declared in the project-owned Target override, keeps an existing Target ID bound to its configured repository-relative path, and re-adopts a nested standalone project that the configuration already names. Enable a generated Validation Gate in that override after reviewing its argv, capability, preparation, timeout, and working directory.

Only `git diff --check` — a read-only check that runs no project code — is enabled without review. Everything else, including `npm run test` and `npx tsc`, is materialized as a disabled Candidate; a repository-local Profile cannot enable one either. Generated commands follow the package manager recorded for that Target, so a `pnpm` Target gets `pnpm run test` and `pnpm exec tsc`, a Bun Target gets `bun run test` and `bun x tsc`, and Yarn gets `yarn run` for both. A gate with `preparation = "locked-install"` runs one hook-free install per ecosystem before the gate itself:

| Package manager | Locked preparation |
|---|---|
| `npm` | `npm ci --ignore-scripts` |
| `pnpm` | `pnpm install --frozen-lockfile --ignore-scripts` |
| `yarn` (classic) | `yarn install --frozen-lockfile --ignore-scripts` |
| `yarn` (Berry) | `yarn install --immutable --mode=skip-build` |
| `bun` | `bun install --frozen-lockfile --ignore-scripts` |
| `uv` | `uv sync --frozen --no-install-workspace --no-build` |
| `pdm` | `PDM_ONLY_BINARY=:all: pdm sync --frozen-lockfile --no-self` |

Berry is detected from `.yarnrc.yml` or a `packageManager` major version of 2 or above. Poetry has no switch that guarantees a hook-free install, so a hook-free Poetry gate reports `policy-unsupported` and blocks instead of installing; set `allow_build_hooks = true` on that gate to accept `poetry install` and its project build hooks.

Unknown configuration keys fail closed. Relative paths resolve from the configuration file. Secrets do not belong in TOML; secret-shaped SSH environment keys are rejected. When `state_dir` is omitted, Touchstone uses an isolated per-repository directory under `$XDG_STATE_HOME/touchstone` or `~/.local/state/touchstone`.

Upgrade an unversioned configuration to v1, then explicitly preview and apply v2:

```bash
touchstone config migrate touchstone.toml
touchstone config migrate-v2 touchstone.toml --timezone UTC --hourly-minute 0 --check
touchstone config migrate-v2 touchstone.toml --timezone UTC --hourly-minute 0 --write
```

Both migrations write a sibling backup before replacing project configuration. V2 migration refuses to overwrite an existing generated file. Stack Profiles, per-Target Validation Gates, and the GitHub Actions backend require v2; local scheduling, `run`, `run-due`, and `resume` do not.

---

### Scheduling and Recovery

Loops accept `hourly@MM`, `daily@HH:MM`, or `weekly@DAY,HH:MM` in the configured IANA timezone. DST is resolved from local wall-clock intent.

```bash
touchstone install-scheduler --dry-run
touchstone install-scheduler
touchstone scheduler-status
touchstone run-due
```

The scheduler is only a Wake Signal. `run-due` transactionally claims a repository-global Due Slot, coalesces missed periods, runs at most one active change at a time, and retries retryable failures up to three times with bounded backoff. The same evaluator is used locally and in GitHub Actions.

`status` is pure and never mutates lifecycle state. Use `reconcile` when you intentionally want to compare recorded candidates with GitHub and record merged, closed, failed, or reaped transitions. A partial publication is resolved only once the pull request actually carries the Loop and escalation labels a complete publication applies; `reconcile` adds a missing label itself and leaves the record unresolved when it cannot:

```bash
touchstone status --json
touchstone reconcile --json
```

Local parked checkpoints print this contract:

```bash
touchstone resume <thread-id> approve|close|reanalyze
```

Approval reruns health and publication gates and refuses a pull request whose reviewed head changed. Close records an operator decision. Reanalyze closes the old candidate and starts from current default-branch state.

Stable exit codes are `0` for completed/no-change/rehearsed, `1` for failed, `3` for blocked or detected drift, and `78` for invalid configuration.

---

### Safety Boundary

Touchstone owns configuration validation, bounded discovery, isolated worktrees, model orchestration, deterministic risk escalation, encrypted checkpoints, append-only lifecycle events, Due Slot claims, and pull-request transitions.

The target repository owns its tests, branch protection, required checks, deployment verification, audit policy, credentials, and final merge. Touchstone creates pull requests; it does not merge them. It does not install remote Profiles, resolve mutable Agent CLI versions at runtime, accept a GitHub App installation whose permissions exceed the documented map, request a broad personal access token, turn on GitHub auto-merging, trigger from pull requests, or treat missing/malformed model output as a clean run.

A dry run prevents publication, not model access. It still runs the configured locked preparation and the Validation Gates of every Target the change reaches. Gate selection uses the recorded dependency graph: the Targets owning the changed paths plus their dependents, widened to every Target the Loop configures whenever a shared or repository-root change cannot be attributed safely. Use only repositories, hosts, models, GitHub accounts, and credentials you are authorised to use. Never include secrets, private repository content, model transcripts, encrypted-state keys, or unredacted diagnostics in public issues.

Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/Misoto22/touchstone/blob/main/SECURITY.md).

---

### Development and Release

```bash
git clone https://github.com/Misoto22/touchstone.git
cd touchstone
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check src tests
uv run touchstone graph --check
uv build
uv run twine check dist/*
```

The current release is [v0.1.2](https://github.com/Misoto22/touchstone/releases/tag/v0.1.2), published as [`touchstone-agent` on PyPI](https://pypi.org/project/touchstone-agent/). GitHub Releases publish through PyPI trusted publishing; the repository stores no PyPI API token.

See [CONTRIBUTING.md](https://github.com/Misoto22/touchstone/blob/main/CONTRIBUTING.md) for the TDD and pull-request workflow and [CHANGELOG.md](https://github.com/Misoto22/touchstone/blob/main/CHANGELOG.md) for user-facing changes.

---

Apache License 2.0 · [LICENSE](https://github.com/Misoto22/touchstone/blob/main/LICENSE)
