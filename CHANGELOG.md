# Changelog

All notable user-facing changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Declarative built-in Profiles for generic, JavaScript, Node.js, TypeScript, React, Next.js, Python, FastAPI, and Django repositories.
- Bounded monorepo Target discovery for npm, pnpm, Yarn, Bun, uv, Poetry, and PDM evidence, including dependency-aware affected scope applied to local, hosted, and rehearsal validation.
- Schema-v2 project/generated configuration ownership, deterministic Profile refresh, and an explicit backup-first v1-to-v2 migration command.
- Structured preparation and Validation Gates with disabled-by-default detected candidates.
- Durable Due Slots shared by native schedulers and GitHub-hosted wake signals.
- Repository-owned GitHub Actions workflow generation, immutable Action pins, encrypted state/candidate artifacts, and split Prepare, Analysis, Verify, Publish, and Snapshot trust stages.
- Resumable owner-controlled GitHub App Manifest setup and hosted setup diagnostics.
- Hash-locked Python Action dependencies and integrity-locked Codex and Claude npm runtimes.
- A credential-free Preparation Stage that installs locked project dependencies and attests them to the repository HEAD, configuration digest, Target set, and lockfiles.

### Removed

- `actions.codex_cli_version` and `actions.claude_code_version`; the hosted Agent CLI version is read from the Action's committed `npm` lockfile. A configuration that still sets either key fails with a message naming that key rather than a bare unknown-key error.

### Changed

- Run outcomes and pull-request lifecycle states are separate machine contracts.
- Publication is PR-only by default; Touchstone no longer enables or requires GitHub auto-merge.
- `status` is read-only, while `reconcile` performs explicit lifecycle reconciliation.
- Operator resume decisions are `approve`, `close`, or `reanalyze` and remain bound to the reviewed candidate.
- Generated configuration records package managers per Target, expresses validation commands in that Target's own package manager, removes stale detected Profiles on refresh, re-adopts nested standalone projects the configuration already names, and supports repository-local declarative detectors.
- Hosted visibility and wake cadence are configurable during initialization; dry runs execute configured preparation and validation before stopping publication.
- Target IDs prefer package identity, survive checkout-directory changes, and retain existing configured IDs by repository-relative path during Profile refresh.
- Locked preparation is hook-free per package manager; Poetry reports a structured `policy-unsupported` result instead of installing with build hooks unless they are explicitly allowed.
- `touchstone actions init` resolves the release tag matching the installed distribution instead of the Action repository's default branch.
- Profiles enable only side-effect-minimal Validation Gates: `git diff --check` runs without review, every command that executes project code stays a disabled Candidate, and a repository-local Profile can no longer enable one.
- `touchstone actions setup --organization` stores App secrets as organization secrets restricted to the selected repository, and later checks read organization and repository secrets together.
- `touchstone doctor` reports a `gh` release older than 2.64, which cannot complete `pr edit` since GitHub sunset Projects (classic) and therefore cannot label a published pull request.

### Security

- Model credentials and GitHub publishing credentials cannot coexist in one hosted stage or model subprocess environment, and locked dependency installation happens only where neither exists yet.
- Hosted candidates bind stable finding identity, base SHA, patch digest, run identity, Loop, and the full effective non-secret configuration; separate Verify and Publish runners prevent candidate-controlled Git state from crossing the credential boundary.
- The publishing App token and installation are restricted to the selected repository and to exactly the required permission map, and partial remote writes block new analysis until explicit reconciliation.
- A Publish job that fails or is cancelled without recording its outcome is reconstructed by Snapshot from the authenticated candidate as a `failed` partial marker.
- Hosted commits are authored by the publishing App's bot identity instead of an identity git synthesizes from the runner's user and hostname.
- `reconcile` no longer treats an existing pull request as a finished publication: a partial write stays unresolved until the Loop and escalation labels a complete publication applies are actually present.
- A partial publication now exits non-zero. A parked thread was read as a completed run before the outcome was consulted, so a publication that opened a pull request and then failed reported exit 0 and every exit-code monitor stayed silent.
- A successful publication records its branch, so an operator can approve a parked draft. Only the partial-failure paths stored it, and a resume verifies the live pull request against the stored branch, so every normally parked hosted draft was impossible to approve.
- A model process is given a replacement environment only where the executor can replace one. Over SSH the assignments were appended to the remote command, overriding the configured remote `PATH` and `HOME` and putting a local API key on a remote command line.
- `scheduler-status` and `uninstall-scheduler` still see the shared wake unit after the last Loop schedule is removed, instead of reporting nothing while an enabled timer keeps firing `run-due`.
- Owner App setup verifies repository scope and permissions with a short-lived App JWT before persisting only a non-secret attestation; later local checks label that evidence as cached.
- Hosted bundles use AES-256-GCM with manifest AAD, fresh nonces, path-safe archives, configuration/Profile lineage checks, and ciphertext digests.
- GitHub App private keys are sent to repository secrets through stdin and are never persisted by Touchstone.
- Generated workflows run only from default-branch schedule or manual dispatch, pin every Action to a 40-character commit SHA, and retrieve durable state/candidates by exact digest or candidate identity rather than artifact-list order.

## [0.1.2] - 2026-08-24

### Fixed

- README images and repository links now render correctly on both GitHub and PyPI.

## [0.1.1] - 2026-08-24

### Fixed

- PyPI and GitHub now show the stable `pipx install touchstone-agent` onboarding path and link the current release.

## [0.1.0] - 2026-08-24

### Added

- Installable `touchstone-agent` distribution with the `touchstone` command.
- Interactive repository discovery, strict versioned configuration, diagnostics, and idempotent setup.
- Append-only finding lifecycle with GitHub reconciliation, safe reaping, status output, and structured run events.
- Independent review, verified auto-merge arming, draft parking, and head-SHA-bound human resume.
- Native launchd and systemd user scheduling with portable hourly, daily, and weekly schedules.
- Built-in generic audit and review briefs packaged in the wheel.
- Per-repository XDG state isolation and target-aware local/SSH diagnostics.
- Online publication preflight for GitHub access, auto-merge, labels, workflows, and branch protection visibility.

### Security

- Removed project-specific paths and personal commit identities from runtime behavior.
- Invalid agent output is inconclusive rather than clean.
- GitHub mutation results are checked before lifecycle state advances.
- Built-in control-path escalation cannot be disabled by project config, and protected-path globs cover environment variants.
- Secret-shaped SSH environment keys and invalid configuration types or ranges are rejected before execution.
- A failed fetch cannot fall back to a stale default-branch worktree.
- Dry runs no longer reconcile or close live pull requests, and persisted failure notes exclude model output.
- GitHub API payloads and native scheduler command results are validated before state advances.

[0.1.2]: https://github.com/Misoto22/touchstone/releases/tag/v0.1.2
[0.1.1]: https://github.com/Misoto22/touchstone/releases/tag/v0.1.1
[0.1.0]: https://github.com/Misoto22/touchstone/releases/tag/v0.1.0
