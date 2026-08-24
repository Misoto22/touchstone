# Encrypt and Lineage State Snapshots

State Snapshots and inter-job candidate bundles are client-side encrypted with a dedicated repository secret, leaving only the schema, run identity, compatibility metadata, and ciphertext digest visible. Every completed or held run creates a new immutable snapshot in a repository-and-Loop Snapshot Lineage; recovery selects a specific compatible historical run, verifies its configuration identity and digest, and otherwise records a Clean Start.
