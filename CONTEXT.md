# Touchstone

Touchstone describes repeatable agent loops that inspect a repository, judge their own work, and publish changes under explicit safety boundaries.

## Language

**Stack Profile**:
A named description of a Project Target's technology stack and its audit expectations, independent of the environment where Touchstone runs.
_Avoid_: Template, framework preset, stack-specific runner

**JavaScript Profile**:
The Stack Profile for a JavaScript package ecosystem or toolchain, whether or not its application executes on Node.js.
_Avoid_: Node Profile, browser runtime

**Node Profile**:
The Stack Profile for a Project Target with evidence that its application or tool executes on the Node.js runtime.
_Avoid_: Any package.json project, JavaScript Profile

**Profile Set**:
The collection of composable Stack Profiles that together describe one Project Target.
_Avoid_: Primary framework, inherited template

**Project Target**:
A repository-relative component boundary that can be detected, configured, and audited independently while remaining part of one repository.
_Avoid_: Separate repository, deployment target, workspace assumption

**Target ID**:
The stable logical identity of a Project Target, independent of its current repository-relative path.
_Avoid_: Target path, Profile name

**Workspace Container**:
A workspace root that organizes member Project Targets but has no independent application source or validation contract of its own.
_Avoid_: Root Target, implicit application

**Stack Detection**:
Evidence-based identification of Project Targets and their Profile Sets when Touchstone is first adopted. A detected Profile is not silently reconsidered during later runs.
_Avoid_: Guessing, runtime framework selection

**Detection Verdict**:
An explainable `confirmed`, `candidate`, or `unsupported` classification paired with the Stack Evidence that produced it.
_Avoid_: Confidence score, unexplained match

**Loop Attachment**:
A named command whose output is appended to a Loop's brief, so a session reads current repository state instead of being told about it.
_Avoid_: Evidence, context substitution, trusted input

**Stack Evidence**:
Repository-owned manifests, lockfiles, and framework configuration that support a Stack Detection result.
_Avoid_: Model guess, filename hunch

**Generic Profile**:
The project-neutral Stack Profile used when no technology-specific Profile is confirmed, preserving repository audit without inventing framework rules.
_Avoid_: Unknown framework guess, initialization failure

**Materialized Profile Configuration**:
The explicit, reviewable project configuration produced from detected Stack Profiles and changed only by a deliberate edit or refresh.
_Avoid_: Hidden runtime defaults, dynamic profile policy

**Generated Block**:
A configuration block owned by Touchstone and reproducible from recorded Profile provenance.
_Avoid_: User-editable defaults, merged ownership

**Override Block**:
A project-owned configuration block that deliberately changes or extends a Generated Block and survives Profile refreshes.
_Avoid_: Generated edit, implicit override

**Run Outcome**:
The result of one requested Touchstone operation: `completed`, `no_change`, `blocked`, `failed`, or `rehearsed`.
_Avoid_: Change status, CLI message

**Change Lifecycle**:
The durable state of a proposed repository change: `proposed`, `awaiting_human`, `awaiting_checks`, `merged`, `closed`, `reaped`, or `failed`.
_Avoid_: Run result, merging, escalated

**Resume Decision**:
A structured human action on an `awaiting_human` change: `approve`, `close`, or `reanalyze`.
_Avoid_: Free-form control comment, arbitrary resume prompt

**Partial Failure**:
A failed run that has already changed external state and therefore retains its identifiers and evidence for reconciliation instead of pretending to roll back.
_Avoid_: Clean rollback, blind retry

**Repository Loop**:
The default Loop created for a repository and bound to all of its Project Targets. It validates only the Targets affected by a proposed change unless the project is deliberately split into narrower Loops.
_Avoid_: Loop per framework, automatic schedule fan-out

**Schedule Timezone**:
The repository's explicit IANA timezone in which Loop schedules are interpreted consistently across every Execution Backend.
_Avoid_: Runner local time, implicit timezone

**Schedule Generation**:
One version of a Loop's cadence and Schedule Timezone whose missed periods are considered together and never replayed into a later schedule definition.
_Avoid_: Workflow revision, cron string

**Wake Signal**:
A scheduler event that asks Touchstone to evaluate which Loops are due; it is not itself proof that a Loop must run.
_Avoid_: Scheduled run, exact timer

**Catch-up Run**:
One current-state audit that represents all missed periods for an overdue Loop and records their count and lateness without replaying each period.
_Avoid_: Backlog replay, historical audit

**Due Slot**:
The idempotent scheduled opportunity identified by a Loop, Schedule Generation, and UTC scheduled time.
_Avoid_: Wake Signal, workflow run

**Durable Claim**:
A time-bounded ownership record acquired before work begins on a Due Slot and recoverable after its lease expires.
_Avoid_: PID lock, permanent consumption

**Validation Gate**:
A bounded, deterministic check whose failure or timeout prevents publication and cannot be waived by the agent that produced the change.
_Avoid_: Agent suggestion, advisory check

**Validation Candidate**:
A Profile-suggested project command that remains disabled until its side effects and runtime requirements are explicitly accepted.
_Avoid_: Default Gate, trusted package script

**Preparation Stage**:
The secret-free stage that creates a locked dependency environment before any model or publishing credential becomes available.
_Avoid_: Agent setup, unrestricted install

**Execution Backend**:
The environment that hosts a Touchstone run, independently of the repository's Stack Profiles. Local execution, SSH execution, and GitHub-hosted Actions are Execution Backends.
_Avoid_: Stack template, deployment target

**State Snapshot**:
A confidential, immutable, restorable record of a hosted run's checkpoints, ledger, and events that can continue a later run.
_Avoid_: Runner cache, mutable shared state

**Snapshot Lineage**:
The ordered sequence of compatible State Snapshots for one repository, Loop, and materialized configuration.
_Avoid_: Latest artifact name, shared mutable snapshot

**Publishing Identity**:
The credentialed actor responsible for Touchstone's pull-request lifecycle. An autonomous GitHub-hosted run uses a GitHub App rather than a person's identity.
_Avoid_: User account, personal token

**Owner App**:
A GitHub App created, installed, and controlled by the repository owner or organization to serve as Touchstone's Publishing Identity.
_Avoid_: Central Touchstone service, personal token

**Partial Setup**:
An interrupted Actions setup whose completed external steps are discovered and reused by a later setup or doctor run.
_Avoid_: Corrupt setup, forced restart

**Mutating Run**:
A Touchstone run permitted to publish repository changes. A repository admits only one Mutating Run at a time, even when its Loops address different Project Targets.
_Avoid_: Parallel writer, cancellable publish

**Clean Start**:
A hosted run that begins without a compatible State Snapshot and records that discontinuity explicitly.
_Avoid_: Silent recovery, implicit resume

**Analysis Stage**:
The hosted trust stage that can use model credentials but has no Publishing Identity and produces a reviewed candidate for publication.
_Avoid_: Write-capable agent job

**Publish Stage**:
The hosted trust stage that can use the Publishing Identity but has no model credentials and accepts only a verified output from the Analysis Stage.
_Avoid_: Model execution, shared-secret job

**Unattended PR Mode**:
The default hosted mode that may create a pull request without per-run approval but does not enable auto-merge by default.
_Avoid_: Unattended merge, approval-free policy

**Approval-Gated Mode**:
An optional hosted mode whose Publish Stage waits for the repository's configured GitHub Environment approval before receiving publishing credentials.
_Avoid_: Default schedule mode, prompt approval
