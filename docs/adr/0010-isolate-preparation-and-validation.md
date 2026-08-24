# Isolate Preparation and Validation

Hosted dependency preparation runs without model, publishing, or project secrets and uses only locked or frozen package-manager operations; Node lifecycle scripts and Python build hooks require explicit enablement. Profiles automatically enable only side-effect-minimal Validation Gates, materialize application commands as disabled Validation Candidates, and reject tracked-file changes while withholding all secrets and external-service credentials during validation.
