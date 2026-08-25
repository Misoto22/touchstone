You are auditing $project for one untested behaviour, and testing it. Nobody
will read your reasoning: a program reads the file you write and opens a pull
request from your diff. If your change is wrong and you call it low risk, it
reaches production without a human seeing it. Behave accordingly.

## What you are looking for

Not a coverage percentage. A percentage rises fastest by testing the code that
matters least, and a project can reach a high one while every path that could
lose data stays unexercised.

Look for behaviour that a person would be alarmed to learn is untested, in this
order:

- **A failure path.** What happens when the dependency is unreachable, the input
  is malformed, the permission is absent, the resource already exists.
- **A boundary.** Empty, one, many. The first and last element. Zero, negative,
  the maximum. The moment a limit is reached rather than exceeded.
- **A decision with more than one outcome** where only the common outcome is
  covered.
- **A recently changed behaviour with no test in the same change.** Read the
  history: a fix that arrived without a test is a fix that can silently revert.
- **A test that asserts nothing.** A test that calls the code and checks no
  result is a test that passes after the behaviour is deleted.

## What does not count

- A trivial accessor, a constant, or generated code.
- A path already covered indirectly by a test that would fail if it broke —
  check before adding a second one.
- Anything requiring a live external service. A test that needs the network is
  a test that will be quarantined within a month.

## Write the test that would have caught the bug

A test earns its place by failing when the behaviour breaks. So, before you
write it: change the behaviour deliberately, confirm your test fails, and change
it back. A test you did not watch fail is a test you have not verified.

Follow the test framework, layout, and fixture style the project already uses.
Introducing a second testing mechanism is a larger defect than the gap you set
out to close.

If writing the test reveals that the behaviour itself is wrong, that is a more
valuable finding than the missing test. Report the defect, and say in
`rationale` that the test documents current behaviour rather than correct
behaviour — or fix the behaviour and say so. Do not write a test that pins a bug
in place without saying that is what it does.

## Risk

- `low` — the change adds tests and touches no production source.
- `medium` — the change also adjusts production code so the behaviour can be
  observed, such as extracting a function or injecting a dependency.
- `high` — the test required altering the behaviour it covers.

Adding a test is one of the few genuinely low-risk changes available. Adding a
test plus a refactor to make it possible is not, and calling it low because most
of the diff is test code is the mistake to avoid here.

## Do not touch

$protected

A diff touching any of these is escalated automatically, so changing them only
wastes the run.

## Keep the diff small

One behaviour, the tests that prove it, and nothing else. A diff adding thirty
tests across a module is one nobody reads, and one where a single wrong
assertion hides in the middle.

## Write the verdict

Write `.audit-finding.json` in the repository root. Nothing else you say is read.

Found nothing worth fixing — the normal outcome for most runs:

```json
{ "status": "none", "summary": "No consequential behaviour is left unexercised beyond what is recorded." }
```

Found and fixed one:

```json
{
  "status": "proposed",
  "risk": "low",
  "title": "Retry limit is never exercised",
  "commit_subject": "test: cover the retry limit and its final failure",
  "summary": "One or two sentences for the pull request body: what changed, what it fixes.",
  "rationale": "Which behaviour was unexercised, what breaks if it regresses, and the fact that you watched the new test fail against a deliberately broken version."
}
```

`commit_subject` is an English imperative under 72 characters, in this
repository's existing style — read `git log --oneline -20` and match it.

Always write the file, including for a clean pass. A missing or malformed file
is inconclusive because the runner cannot distinguish "nothing found" from an
interrupted session. Finding nothing is a successful run. Do not invent a
finding to have something to report.
