# Fleet Configuration and Self-Hosted Execution Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Every task starts with a failing test.

**Goal:** Let one project fan out from a central configuration to many repositories, let each Loop name the engine it runs on, give Rust and .NET first-class Stack Profiles alongside concern-shaped Briefs, open per-Loop auto-merge only where an independent Verify stage exists, and make a container a third execution backend.

**Architecture:** A fleet repository holds `projects/<name>.toml`. `touchstone sync` renders one `.touchstone/fleet.toml` per member repository and proposes it through the existing pull-request lifecycle; the repository's own `touchstone.toml` names that file with `extends` and overrides any key it disagrees with. Engines become a named pool keyed by name, so `[engine]` and `[engine.<name>]` coexist inside schema v2. Concern Briefs stay technology-neutral and read structured naming rules injected by Stack Profiles, so the brief count stays additive rather than multiplicative. Container execution reuses `run-due` unchanged: one repository per container, one state volume per container, no shared credential.

**Tech Stack:** Python 3.12+, dataclasses, `tomllib`, `tomli-w`, pytest, Docker Compose

**Grilled design record:** this plan's decisions were settled in a design interview; each Task names the alternative it rejected and why.

## Global Constraints

- Schema v2 stays loadable and no v3 is introduced. Unknown keys keep failing closed.
- `[engine]` with scalar keys and `[engine.<name>]` subtables coexist; the loader distinguishes them structurally, never by guessing.
- The fleet never writes to a member repository directly. `sync --check` is read-only and exits `3` on drift; `sync --pr` proposes through the pull-request lifecycle. No `--push`.
- Configuration carries `op://` references, never credential values. Touchstone resolves a reference only inside the process that needs it, and never creates a forge secret on the operator's behalf.
- Validation Gate authorization stays in the member repository's `.touchstone/generated.toml`. The fleet cannot enable a Gate.
- auto-merge requires a backend with an independent Verify stage. Backends without one report `policy-unsupported` and block.
- One execution unit sees exactly one repository.
- Every behavior starts with a failing test and ends with a focused commit.

---

### Task 1: Configurable primary author and co-author trailer

**Rejected alternative:** four free-form name/email fields, which permit configuring the author and co-author as the same identity.

**Files:**
- Modify: `src/touchstone/config.py`
- Modify: `src/touchstone/lifecycle.py`
- Modify: `src/touchstone/nodes/publish.py`
- Modify: `src/touchstone/hosted/runtime.py`
- Modify: `touchstone.example.toml`
- Test: `tests/test_config.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Produces: `GitConfig.author: Literal["bot", "operator"]`
- Produces: `GitConfig.operator_name` / `GitConfig.operator_email`
- Produces: `co_authored_by(config: GitConfig, bot: tuple[str, str]) -> str`
- Preserves: existing `author_name` / `author_email` as the operator identity when `author = "operator"`

- [x] **Step 1: Write failing tests for the trailer and the identity swap**
- [x] **Step 2: Add `author` to `GitConfig` with `bot` as the default and paired-field validation**
- [x] **Step 3: Emit the `Co-Authored-By` trailer from one helper used by both the local and hosted publication paths**
- [x] **Step 4: Replace the hardcoded bot identity in the hosted path with the configured choice**
- [x] **Step 5: Update the example configuration and the README publication description**

---

### Task 2: Named engine pool

**Rejected alternative:** a per-Loop inline engine block, which repeats one endpoint across every Loop that shares it.

**Files:**
- Modify: `src/touchstone/config.py`
- Modify: `src/touchstone/hosted/workflow.py`
- Modify: `src/touchstone/doctor.py`
- Modify: `touchstone.example.toml`
- Test: `tests/test_config.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Produces: `Config.engines: dict[str, EngineConfig]`
- Produces: `Config.engine_for(loop: str) -> EngineConfig`
- Produces: `EngineConfig.api_key_env` and `EngineConfig.api_key_ref`
- Preserves: `Config.engine` as the pool member named `default`

- [x] **Step 1: Write failing tests for `[engine.cheap]` resolution and for a Loop naming an unknown engine**
- [x] **Step 2: Parse scalar `[engine]` keys and `[engine.<name>]` subtables into one pool**
- [x] **Step 3: Resolve `loop.<name>.engine` through the pool and keep `loop.<name>.model` as the narrower override**
- [x] **Step 4: Map only the secrets the configured engines actually name into each Analysis job**
- [x] **Step 5: Report a missing secret in `doctor` with the exact `op read | gh secret set` command, without running it**

---

### Task 3: Rust and .NET Profiles, concern Briefs, structured naming rules

**Rejected alternative:** one built-in brief per concern per stack, which multiplies to fifty-four files for nine profiles and six concerns.

**Files:**
- Create: `src/touchstone/resources/profiles/rust.toml`
- Create: `src/touchstone/resources/profiles/dotnet.toml`
- Create: `src/touchstone/resources/briefs/hardcode.md`
- Create: `src/touchstone/resources/briefs/naming.md`
- Create: `src/touchstone/resources/briefs/error-handling.md`
- Create: `src/touchstone/resources/briefs/test-coverage.md`
- Modify: `src/touchstone/profiles/`
- Test: `tests/test_profiles.py`

**Interfaces:**
- Produces: Cargo and MSBuild Stack Evidence with explainable verdicts
- Produces: `Profile.naming: dict[str, str]` consumed by the naming brief
- Preserves: every Gate that runs project code stays a disabled Candidate

- [x] **Step 1: Write failing detection tests for Cargo workspaces and `.csproj` targets**
- [x] **Step 2: Add both Profiles with their evidence rules and disabled Gate candidates**
- [x] **Step 3: Declare naming rules as data on each Profile**
- [x] **Step 4: Write the four concern Briefs against injected context, naming no technology**
- [x] **Step 5: Verify a Rust repository and a .NET repository each compose a non-generic Profile Set**

---

### Task 4: Per-Loop auto-merge behind explicit gates

**Rejected alternative:** a single global switch, which lets a low-risk Loop's convenience vouch for a high-risk Loop.

**Files:**
- Create: `docs/adr/0022-confine-auto-merge-to-independently-verified-backends.md`
- Modify: `src/touchstone/config.py`
- Modify: `src/touchstone/lifecycle.py`
- Modify: `README.md`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Produces: `LoopConfig.auto_merge: bool`
- Produces: `auto_merge_verdict(...) -> AutoMergeVerdict` recording every unmet condition

- [x] **Step 1: Write failing tests for each of the six conditions and for the unsupported-backend block**
- [x] **Step 2: Add the per-Loop field, defaulting to false**
- [x] **Step 3: Evaluate the conditions as one explainable verdict, never a boolean**
- [x] **Step 4: Block with `policy-unsupported` on a backend without an independent Verify stage**
- [x] **Step 5: Record the decision in an ADR and revise the README Safety Boundary**

---

### Task 5: Central project configuration and `touchstone sync`

**Rejected alternative:** a central process auditing every repository, which concentrates the write access of a whole fleet in one credential domain.

**Files:**
- Create: `src/touchstone/fleet.py`
- Create: `docs/adr/0023-fan-out-fleet-configuration-through-pull-requests.md`
- Modify: `src/touchstone/config.py`
- Modify: `src/touchstone/cli.py`
- Create: `touchstone-project.example.toml`
- Test: `tests/test_fleet.py`

**Interfaces:**
- Produces: `load_project(path: Path) -> ProjectConfig`
- Produces: `render(project: ProjectConfig, member: str) -> str`
- Produces: `touchstone sync --check` (exit `3` on drift) and `touchstone sync --pr`
- Produces: `extends` resolution where a repository key overrides a fleet key

- [x] **Step 1: Write failing tests for rendering, for `extends` precedence, and for drift detection**
- [x] **Step 2: Define the project schema: members, shared Loops, per-member overrides**
- [x] **Step 3: Render `.touchstone/fleet.toml` deterministically so an unchanged project produces an identical file**
- [x] **Step 4: Resolve `extends` in the loader, with the repository's own keys winning**
- [x] **Step 5: Refuse to render a Gate enablement, an `op://` value, or any key the fleet may not own**
- [x] **Step 6: Propose changes through the pull-request lifecycle and provide no direct-write path**

---

### Task 6: Container execution backend

**Rejected alternative:** one container iterating every repository, which is the central-execution shape under another name.

**Files:**
- Create: `Dockerfile`
- Create: `src/touchstone/execution/container.py`
- Create: `docs/adr/0024-run-one-repository-per-container.md`
- Modify: `src/touchstone/fleet.py`
- Modify: `src/touchstone/doctor.py`
- Test: `tests/test_container.py`

**Interfaces:**
- Produces: a base image carrying Touchstone, the locked Agent CLI, and Git, and nothing project-specific
- Produces: a supervisor loop calling `run-due`, with no second clock
- Produces: `touchstone sync` emitting one Compose service per member repository

- [x] **Step 1: Write failing tests for the supervisor loop and for the rendered Compose file**
- [x] **Step 2: Build the base image and pin the Agent CLI from the committed lock**
- [x] **Step 3: Drive `run-due` from an idempotent in-process loop rather than cron**
- [x] **Step 4: Render one service per member, each with its own state volume and credential set**
- [x] **Step 5: Report `policy-unsupported` when a container-backed Loop requests auto-merge**
