# Security Policy

## Supported versions

Security fixes are applied to the latest released minor version.

## Reporting a vulnerability

Report vulnerabilities privately through the repository's **Security** tab:

1. Open **Security advisories**.
2. Select **Report a vulnerability**.
3. Include the affected version, impact, minimal reproduction, and any proposed mitigation.

Do not open a public issue for a suspected vulnerability. Do not include access tokens, private repository content, model transcripts, environment values, or unredacted diagnostic output.

You should receive an acknowledgement within seven days. The advisory will remain private while the impact is reproduced, a fix is prepared, and a coordinated disclosure date is agreed.

## Scope

Security reports may cover command execution boundaries, secret exposure, unsafe Git or GitHub mutations, authorization assumptions, scheduler generation, checkpoint integrity, and publication or resume policy bypasses.

Touchstone does not own security policy, branch protection, credentials, or deployment controls in a target repository. Reports about those systems belong to their respective owners unless Touchstone bypasses or misrepresents them.
