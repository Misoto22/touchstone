# Use an Owner App and Explicit Actions Setup

Each adopting user or organization creates and installs its own Owner App through an interactive manifest flow; Touchstone does not operate a central credential service. `touchstone actions init` only generates reviewable repository files, while `touchstone actions setup` performs confirmed browser handoffs, in-memory private-key transfer to repository or organization secrets, and verification; Unattended PR Mode is the portable default and Approval-Gated Mode is an optional GitHub Environment enhancement.
