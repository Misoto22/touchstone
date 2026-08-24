# Separate Generated Configuration from Project Overrides

Materialized Profile Configuration keeps provenance-bearing Generated Blocks separate from project-owned Override Blocks; refresh replaces generated content, preserves overrides, and presents a diff instead of attempting a textual three-way merge. Validation Gates use structured arguments, Target working directories, timeouts, preparation requirements, and declared capabilities, with shell execution available only as an explicit risk-bearing exception.
