# Changelog

All notable user-facing changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Declarative built-in Profiles for generic, JavaScript, Node.js, TypeScript, React, Next.js, Python, FastAPI, and Django repositories.
- Bounded monorepo Target discovery for npm, Yarn, pnpm, uv, and PDM workspaces, including dependency-aware affected scope.
- Schema-v2 project/generated configuration ownership, deterministic Profile refresh, and an explicit backup-first v1-to-v2 migration command.
- Structured preparation and Validation Gates with disabled-by-default detected candidates.
- Durable Due Slots shared by native schedulers and GitHub-hosted wake signals.
- Repository-owned GitHub Actions workflow generation, immutable Action pins, encrypted state/candidate artifacts, and split Prepare, Analysis, Verify, Publish, and Snapshot trust stages.
- Resumable owner-controlled GitHub App Manifest setup and hosted setup diagnostics.

### Changed

- Run outcomes and pull-request lifecycle states are separate machine contracts.
- Publication is PR-only by default; Touchstone no longer enables or requires GitHub auto-merge.
- `status` is read-only, while `reconcile` performs explicit lifecycle reconciliation.
- Operator resume decisions are `approve`, `close`, or `reanalyze` and remain bound to the reviewed candidate.
- Generated configuration records package managers per Target, removes stale detected Profiles on refresh, and supports repository-local declarative detectors.
- Hosted visibility and wake cadence are configurable during initialization; dry runs execute configured preparation and validation before stopping publication.

### Security

- Model credentials and GitHub publishing credentials cannot coexist in one hosted stage.
- Hosted candidates bind stable finding identity, base SHA, patch digest, run identity, Loop, and the full effective non-secret configuration; publication credentials are minted only after credential-free validation.
- The publishing App token and installation are restricted to the selected repository, and partial remote writes block new analysis until explicit reconciliation.
- Hosted bundles use AES-256-GCM with manifest AAD, fresh nonces, path-safe archives, configuration/Profile lineage checks, and ciphertext digests.
- GitHub App private keys are sent to repository secrets through stdin and are never persisted by Touchstone.
- Generated workflows run only from default-branch schedule or manual dispatch and pin every Action to a 40-character commit SHA.

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
