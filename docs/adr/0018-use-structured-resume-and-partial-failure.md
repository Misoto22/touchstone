# Use Structured Resume and Preserve Partial Failure

Hosted resume accepts only the `approve`, `close`, and `reanalyze` Resume Decisions bound to a specific Loop, candidate, and Snapshot Lineage; free-form comments never control execution. If an external write succeeds before a later operation fails, Touchstone records a Partial Failure, preserves branch and remote identifiers, and reconciles that evidence before any idempotent continuation instead of deleting or blindly duplicating it.
