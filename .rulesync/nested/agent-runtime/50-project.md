---
root: true
targets: ["agentsmd"]
description: Project-owned rules for Touchstone agent runtimes
scope: nested
---

# agent-runtime

- **[TOUCH-AGENT-001] MUST — Keep each runtime independently locked.** Claude and Codex runtime packages retain their own committed lockfiles and must not float dependencies during an unattended run.
- **[TOUCH-AGENT-002] MUST — Preserve the engine boundary.** Runtime adapters translate the shared Touchstone candidate contract without moving verification, publishing authority, or another engine's credential into the model process.
