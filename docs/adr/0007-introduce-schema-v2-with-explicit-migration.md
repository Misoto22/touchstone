# Introduce Schema v2 with Explicit Migration

Project Targets, Profile provenance, and Validation Gates enter a new schema v2 while schema v1 remains readable with its original meaning. `touchstone migrate` must preview and explicitly write the upgrade instead of silently changing a committed project's behavior.
