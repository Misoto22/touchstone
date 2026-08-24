## Change

Describe the user-visible behavior or invariant changed.

## Test first

Name the failing test added before the implementation and what it proves.

## Safety and compatibility

Describe effects on configuration, credentials, Git/GitHub mutations, lifecycle state, and migration.

## Verification

```text
uv run pytest
uv run ruff check .
uv run touchstone graph --check
uv build
```

- [ ] No secrets, personal paths, or project-specific operational defaults were added.
- [ ] Documentation and changelog match the implemented command surface.
- [ ] The graph diagram is current when graph edges changed.
