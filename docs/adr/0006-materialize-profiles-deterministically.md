# Materialize Profiles Deterministically

Touchstone creates one default Repository Loop across detected Project Targets and materializes Profile values using semantic precedence: explicit project configuration, repository-local declarative Profiles, framework Profiles, then language or runtime Profiles. Lists merge without duplicates, unsafe scalar conflicts fail visibly, local Profiles cannot execute detector code, and unverified framework majors fall back to supported base Profiles with a warning.
