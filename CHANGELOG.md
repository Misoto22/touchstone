# Changelog

All notable user-facing changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A project covers many repositories. A project file holds the Loops, engines, and schedules that are the same everywhere; `touchstone sync --check` reports which members differ from what it renders, and `touchstone sync --pr` proposes each drifted member's fragment as a pull request. Members name that fragment with `extends` and override any key they disagree with, so precedence runs from machine-owned evidence, through the fleet's decisions, to the repository's own word. There is no direct-write path, a project may not set `target`, `generated`, `project`, `state_dir`, or `version`, and a credential-shaped key that is not an `op://` reference is refused.
- A container backend, one repository per container. `touchstone sync --compose` renders one service per member, each with its own checkout, state volume, and credential set. The image carries Touchstone, `git`, `gh`, and the Agent CLI from its committed lockfile and nothing a project owns; a repository needing a toolchain derives from it. The supervisor is a fixed-interval wake signal over the existing `run-due`, not a second clock, and a failed wake is a failed run rather than a failed supervisor.
- A named engine pool. Any subtable of `[engine]` other than `budget` is a pool member, and `loop.<name>.engine` chooses one, so a Loop hunting hardcoded values can run on a cheap model while one judging naming runs on a strong one — or on another provider. `[engine]` remains the member called `default`, so this stays schema v2. A member carries `api_key_env`, since two members can speak the same API and hold different keys; the session receives that value under the variable its own CLI reads and never learns the original name. A named variable that is absent fails rather than falling back to the vendor key. `api_key_ref` records where the operator's own store holds it, and `doctor` prints the command that would resolve it without ever running one.
- Per-Loop `auto_merge`, off by default. Arming requires low risk, an approving independent review, a ready pull request, passing Validation Gates, a repository that allows auto-merge, a declared required workflow, and a protected base branch; any unmet condition leaves the pull request for a person and records which condition it was. A backend without an independent Verify stage refuses such a Loop with `policy-unsupported` before pushing a branch, rather than silently opening an ordinary pull request.
- `git.author` chooses which of two identities git records as the author of a published commit — the bot that ran the Loop or the configured operator — and credits the other in a `Co-Authored-By` trailer. A configuration without an operator pair behaves exactly as before.
- Built-in `rust` and `dotnet` Stack Profiles, detected from a Cargo manifest and a project file, with their gates materialized as disabled candidates.
- Concern briefs `builtin:hardcode`, `builtin:naming`, `builtin:error-handling`, and `builtin:test-coverage`, which name no language or framework: a Profile declares its naming conventions in a `[naming]` table, `profile refresh` renders a Target's whole Profile Set into the generated Loop context, and the brief reads them from there.

- `engine.base_url` routes model calls to a self-hosted or third-party endpoint instead of the vendor's own, with `engine.wire_api` selecting the HTTP shape for Codex. Claude reads the address from `ANTHROPIC_BASE_URL`; Codex is given a named provider so the address, the wire shape, and the variable holding the key are declared together and the key itself never reaches a command line. The address must be https unless it is loopback, and may carry no query string or userinfo.

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

### Fixed

- `touchstone actions setup` stores the one-time App private key before verifying the installation, so an install a person has not finished yet no longer consumes a key GitHub cannot reissue. A verdict that condemns the App — wrong permissions or a wider repository scope — still takes the stored key back, so a rejected App leaves nothing a workflow could mint a token from.
- `touchstone actions setup` can register an Owner App. The manifest omitted `hook_attributes.url`, which GitHub requires even for an inactive webhook, so registration was rejected outright; the CSRF state was also sent as a form field rather than in the action URL, so the callback carried no state to check.
- A parked draft no longer stops the code audit. Whether an open draft holds a loop's pull-request slot was inferred from `require_change_under` being set, which named the harness review exactly as long as it was the only loop with source paths to maintain; generated stack evidence began setting them for the code audit, and the inference started reporting the opposite of what it meant. The code audit parks every medium-risk finding as a draft, and a parked draft waits for a person and is never reaped, so its first medium-risk finding held the slot against every run that followed. The policy is now the loop's own `drafts_hold_slot`, false by default, and a migrated v1 harness review keeps its "never more than one open at a time".
- The base Profile's Validation Gates reach a Target whose stack was detected. `generic` is attached as a Match only when nothing else matches, so composing Gates from Matches alone left `git diff --check` — the one Gate any Profile enables without operator review — reaching exactly the repositories Touchstone could not identify. On every repository it could, `touchstone validate` reported every Gate as `disabled` and ran nothing.
- Generated source paths describe the Target that is there rather than the layout its Profile guesses at. Absent directories are dropped, and a Python package that sits beside `pyproject.toml` instead of under `src/` is found, so `require_change_under` no longer names three directories a flat-layout Target does not have — which discarded every source-only change the loop made to it, after the audit that found it had already been paid for.
- A source path is matched at a directory boundary. Scoping a Profile's `src/` to a Target dropped the separator, and the consumer compared bare string prefixes, so `apps/web/apple.ts` counted as a change under `apps/web/app`.
- A command that is not installed is an exit code rather than a traceback. `touchstone doctor` died on the call it makes to check the repository when `gh` was missing — the one prerequisite it is most likely to be run to diagnose, and one it already has a check for.
- Hosted artifact downloads follow GitHub's redirect to signed storage without carrying the API token, which that host rejects. Every download failed before, so a restorable State Snapshot read as absent, every hosted run began as a Clean Start, and a hosted resume could never find its candidate.
- A blocked or failed hosted stage said why. It returned its exit code without printing anything, so a runner log showed only `Process completed with exit code 3` and the reason existed solely inside an uploaded artifact.

### Known gaps

- Publication has never run on a GitHub Actions runner. Prepare, Analysis, and Snapshot have, along with the artifact round-trip and the per-stage credential boundaries; minting the App token, the Publish stage, and recovery from a partial publication remain unproven end to end because each needs an installed Owner App.

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
