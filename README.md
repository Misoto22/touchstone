# harness-loop

Scheduled agent loops that audit a repository, and the harness that judges it.

Two loops share one graph. The **code audit** takes the first open row off a
findings ledger and proposes a fix. The **harness review** checks the rules
themselves against the repository — that every enforcer exists and ran, that
every ratchet is still under its ceiling, that no rule is cited which does not
exist. Both open pull requests; only the safest merge without a person.

## What it will and will not ship

Risk decides, and only the lowest class merges itself:

| class | example | what happens |
|---|---|---|
| `low` | an internal refactor, a deduplicated constant | reviewed by a second session, then auto-merged if approved |
| `medium` | an API change, a backward-compatible migration | parked as a draft, and the thread waits |
| `high` | auth, payments, deletion, a destructive migration | parked as a draft, and the thread waits |

Three checks can only ever raise the class, never lower it: a diff touching a
protected path, a diff straying outside the loop's remit, and a translated
document that moved without its twin. Each exists because a session understated
what it was doing, and a check that can be argued down is not a check.

## The graph

See [docs/graph.md](docs/graph.md), which is generated from the code — `harness-loop
graph --check` fails when the picture stops matching the edges.

`park` is an interrupt, not an exit. The thread is checkpointed to SQLite and
stops; answering on the pull request resumes it at that node rather than
starting a fresh twenty-minute audit. That is what "waits for a person" should
always have meant.

```bash
harness-loop run code                 # one iteration
harness-loop run harness --dry-run    # everything except publishing
harness-loop resume code-audit/… merge
langgraph dev                         # Studio: live state, time travel, resume
```

## Nothing is hardcoded

Engine, model, effort, budget, and **where the work runs** are configuration.
See [harness-loop.example.toml](harness-loop.example.toml).

```toml
[engine]
name = "codex"          # codex | claude
model = "gpt-5.6-sol"
audit_effort = "high"

[execution]
target = "ssh"          # local | ssh
```

`local` and `ssh` are one interface, and nothing above it can tell which it
has. That matters more than it looks: a laptop sleeps, so a schedule there
means "whenever the lid opens" — and telling a latent defect from a live one
needs the production database, which only the server can reach.

The engines are not equivalent, and each says so in its own type:

| | reports cost | enforces paths |
|---|---|---|
| Claude | yes | yes, a per-path deny list |
| Codex | **no** | **no**, a sandbox granting the whole worktree |

On Codex the spend is invisible and the diff check is the only thing between a
stray edit and a protected path. It still catches one — after the fact, by
escalating — and that is worth knowing before choosing.

## Tests

`tests/test_acceptance.py` is not a unit suite. Every case in it is a bug that
reached production or a near miss found by running the thing unattended, and
each was originally invisible: an exit code with an empty log, a session that
hung only when stdin was a pipe, a check that passed because a query returned
nothing.
