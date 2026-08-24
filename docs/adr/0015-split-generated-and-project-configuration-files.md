# Split Generated and Project Configuration Files

Schema v2 stores reproducible Profile output in the machine-owned `.touchstone/generated.toml` and keeps project choices and Override Blocks in the root `touchstone.toml`, which explicitly references the generated file. This physical ownership boundary lets refresh replace generated content without rewriting project comments, order, or deliberate overrides.
