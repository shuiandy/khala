# Security policy

Khala stores what people and their agents remember, so it is a good target. Reports are welcome.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting on this repository
(**Security** tab → **Report a vulnerability**). Include the version or commit, the steps to reproduce, and what
an attacker gains.

You should get a first answer within a week. Fixes are released as soon as they are ready, and the advisory
credits you unless you ask otherwise.

## Supported versions

Only the latest release gets security fixes.

## What counts

In scope, for example:

- reading or changing records, notes, proposals or history outside the scopes a person or agent was granted,
  including learning that such a record exists;
- signing in, finishing OAuth consent or using a token without the required factors;
- cross-site request forgery or script injection in the web admin;
- getting a secret into the repository past the pre-receive hook;
- anything that lets one agent act as another.

Out of scope: denial of service by volume, attacks that need an admin account or root on the server, and
findings in dependencies that Khala does not reach (report those upstream).

## Design notes for reviewers

- Effective access is the person's role in a scope, narrowed by the agent's ceiling and by fixed rules (pinned
  records, auto-loaded scopes). Records an account cannot read are treated as missing.
- The web admin serves no inline scripts and a strict Content Security Policy; state-changing requests need a
  CSRF token and a same-origin check.
- TOTP secrets are encrypted with a key that lives outside the database; tokens and recovery codes are stored
  as hashes.
