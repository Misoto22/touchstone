---
root: true
targets: ["agentsmd"]
description: Project-owned rules for Touchstone test fixtures
scope: nested
---

# tests/fixtures

- **[TOUCH-FIX-001] MUST — Keep fixtures inert and synthetic.** Fixture tokens, repository states, configuration, and outputs are test data only; never copy live credentials, personal data, or private infrastructure values here.
- **[TOUCH-FIX-002] MUST — Update the consuming test.** A fixture change must name and pass the test that exercises its intended failure or success path.
