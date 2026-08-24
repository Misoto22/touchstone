# Separate Stack Profiles from Execution Backends

Touchstone represents repository technology through composable Stack Profiles and represents the place a run is hosted through a separate Execution Backend. Keeping these dimensions independent avoids a growing cross-product such as `nextjs-actions` and `django-ssh`, and lets the same detected repository knowledge apply to local, SSH, and GitHub-hosted runs.
