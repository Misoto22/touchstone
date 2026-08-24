# Make Blocked and Failed Runs Visible

CLI and hosted runs return success only for `completed`, `no_change`, and `rehearsed`; `blocked` exits 3, `failed` exits 1, command usage remains 2, and invalid configuration remains 78. An engine or structured-output failure becomes `failed` after bounded attempts, a failed Validation Gate or unmet safety prerequisite becomes `blocked`, and a policy escalation remains a successful run whose Change Lifecycle is `awaiting_human` or `awaiting_checks`.
