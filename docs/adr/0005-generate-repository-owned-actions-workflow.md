# Generate a Repository-Owned Actions Workflow

Touchstone generates a thin, reviewable workflow in the target repository that invokes a pinned first-party composite action; project schedules remain in `touchstone.toml`, and the workflow only wakes Touchstone to run due Loops. Mutating Runs share one repository-wide non-cancelling concurrency boundary, default to creating a pull request without auto-merge, use provider API keys from Actions secrets, and retain restorable State Snapshots for 90 days before an explicit Clean Start.
