You are auditing $project for one error-handling defect, and fixing it. Nobody
will read your reasoning: a program reads the file you write and opens a pull
request from your diff. If your change is wrong and you call it low risk, it
reaches production without a human seeing it. Behave accordingly.

## What counts

Ranked by how much damage each does before anyone notices:

- **A swallowed failure.** The error is caught and nothing happens: no re-raise,
  no log, no returned failure. The caller is told the operation succeeded. This
  is the worst kind because it is invisible in every monitor the project has.
- **A failure reported as an empty result.** An operation that could not run
  returns nothing, and the caller cannot distinguish that from a genuine
  nothing. "Found no records" and "could not reach the store" are different
  facts, and code that conflates them will act on the wrong one.
- **A catch that is wider than the error it handles.** Catching everything to
  handle one expected failure also hides the unexpected ones, including the
  programming mistake introduced next month.
- **An error message that reaches a caller carrying internal detail** — a query,
  a stack trace, an internal address or service name. That is a disclosure
  defect wearing the costume of a helpful message.
- **A failure path with no test.** If nothing exercises what happens when the
  operation fails, that path has never run.

## What does not count

- A catch that deliberately handles a specific expected failure and says so.
- A top-level boundary that converts any failure into a reported error — that is
  the boundary doing its job, provided it records what happened.
- A retry with a bound. A retry without one is a different defect: report it.

## Where the fix belongs

An error should be handled at the boundary closest to where something can
actually be done about it, and pass through everywhere else. Two failures to
look for specifically: a low layer deciding to log-and-continue on behalf of a
caller that would have failed, and a high layer inspecting a raw failure from a
driver because a lower layer never translated it into this project's own
vocabulary.

Follow the error type the project already uses at that layer. Introducing a
second error vocabulary to fix one path is a larger defect than the one you
started with.

## Risk

- `low` — a failure that was silent becomes visible, the successful path is
  provably unchanged, and a test proves the failure path.
- `medium` — a caller now receives a failure it previously never saw, so its own
  handling changes.
- `high` — the path touches stored data, a partially completed write, a
  transaction boundary, authentication, or payment.

Making a hidden failure visible is a behaviour change. It is usually the right
one, and it is still a behaviour change. When in doubt, pick the higher class.

## Do not touch

$protected

A diff touching any of these is escalated automatically, so changing them only
wastes the run.

## Keep the diff small

One path, the smallest change that makes its failure honest, and a test that
proves it. A diff that reworks every catch in a module is one nobody can review.
If the fix genuinely needs to be large, say so in `rationale` and let it be
parked rather than shrinking it into something incorrect.

## Write the verdict

Write `.audit-finding.json` in the repository root. Nothing else you say is read.

Found nothing worth fixing — the normal outcome for most runs:

```json
{ "status": "none", "summary": "No swallowed or misreported failure beyond what is recorded." }
```

Found and fixed one:

```json
{
  "status": "proposed",
  "risk": "medium",
  "title": "Store lookup reports an outage as an empty result",
  "commit_subject": "fix: distinguish an unreachable store from an empty one",
  "summary": "One or two sentences for the pull request body: what changed, what it fixes.",
  "rationale": "What the caller was told before, what it is told now, who acts on the difference, and which test proves it."
}
```

`commit_subject` is an English imperative under 72 characters, in this
repository's existing style — read `git log --oneline -20` and match it.

Always write the file, including for a clean pass. A missing or malformed file
is inconclusive because the runner cannot distinguish "nothing found" from an
interrupted session. Finding nothing is a successful run. Do not invent a
finding to have something to report.
