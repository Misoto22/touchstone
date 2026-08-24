# Treat Schedulers as Wake Signals

Loop schedules use one explicit repository IANA timezone across all Execution Backends, while GitHub cron and native timers provide only Wake Signals to a common due evaluator. Public hosted repositories default to a configurable 15-minute off-hour wake and private repositories to 60 minutes; missed periods coalesce into one Catch-up Run, a Clean Start establishes an immediate baseline, and a new Schedule Generation never replays periods from the old definition.

Manual dispatch follows the same default-branch due path unless it explicitly forces one Loop, and multiple due Loops run in stable priority and ID order. Processing continues after `no_change`, but an active proposed change or repository-wide blocked or failed outcome stops the wake and leaves remaining Loops due.
