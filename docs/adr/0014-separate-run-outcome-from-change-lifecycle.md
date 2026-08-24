# Separate Run Outcome from Change Lifecycle

Touchstone reports a Run Outcome (`completed`, `no_change`, `blocked`, `failed`, or `rehearsed`) separately from the durable Change Lifecycle (`proposed`, `awaiting_human`, `awaiting_checks`, `merged`, `closed`, `reaped`, or `failed`). Commands that inspect status remain read-only, while the explicitly mutating `reconcile` operation advances lifecycle state from GitHub evidence; terms such as `held`, `merging`, and `escalated` no longer serve as machine contracts.
