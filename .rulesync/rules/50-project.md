---
root: true
targets: ["agentsmd"]
description: Project-owned rules for Touchstone
scope: project
---

# Touchstone

- **[TOUCH-ARCH-001] MUST — Keep unattended work fail-closed.** Durable schedules, candidate lineage, verification attestations, publishing authority, and recovery state remain separated across the documented stages.
- **[TOUCH-SEC-001] MUST — Preserve credential isolation.** Project code and preparation gates do not receive model, state, or publishing credentials; each stage receives only its documented credential and sanitized environment.
- **[TOUCH-CONF-001] MUST — Respect configuration ownership.** `touchstone.toml` is project-owned, `.touchstone/generated.toml` is machine-owned, unknown keys fail closed, and generated configuration changes use the supported migration or profile commands.
- **[TOUCH-GATE-001] MUST — Keep executable validation explicit.** New validation commands start disabled, require reviewed capabilities and preparation, and never become passing evidence when unsupported or inconclusive.
- **[TOUCH-TEST-001] MUST — Run package quality gates.** Use the uv-managed development environment and run pytest, Ruff, packaging, and repository-specific integration checks applicable to the change.
