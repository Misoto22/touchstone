# Contributing to Touchstone

## Development setup

Prerequisites: Python 3.12 or 3.13, Git, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Misoto22/touchstone.git
cd touchstone
uv sync --all-groups
```

## Workflow

1. Create a focused branch. Codex-authored branches use the `codex/` prefix.
2. Add a failing test that demonstrates the behavior change.
3. Implement the smallest change that makes the test pass.
4. Refactor only while the suite remains green.
5. Update the README, example config, graph, or changelog when their public contract changes.

English is used for code identifiers, comments, documentation, and commit messages. Match the existing module boundaries and avoid project-specific paths, repository names, credentials, models, workflows, or personal identities as operational defaults.

## Verification

Run the same local gates used by CI:

```bash
uv run pytest
uv run ruff check .
uv run touchstone graph --check
uv build
```

Distribution changes must also install the built wheel into an isolated environment and run `touchstone --help` and `touchstone graph` outside the source checkout.

## Pull requests

Keep pull requests incremental and explain:

- the invariant or user behavior being changed;
- the failing test added first;
- safety and compatibility implications;
- exact verification commands and results.

Never force-push `main`. Do not weaken tests, branch protection, required checks, or secret scanning to make a change pass.

By contributing, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
