# Keep Hosted Operation within GitHub

GitHub-hosted runs persist immutable State Snapshots as Actions artifacts and use a GitHub App installation identity for unattended publishing, required checks, and policy-driven merge. This avoids requiring an external state service or a personal access token; the repository `GITHUB_TOKEN` remains suitable for non-autonomous dry-run and reporting modes.
