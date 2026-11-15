# Security Policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's
[private vulnerability reporting](https://github.com/veteranbv/inky-bird/security/advisories/new)
and include the affected revision, impact, reproduction steps, and any proposed
mitigation. Remove unrelated credentials, locations, and personal data.

What to expect:

- The maintainer acknowledges your report within 7 days.
- Disclosure is coordinated with you. The goal is a fix and public disclosure
  within 90 days of your report. Please do not disclose an unresolved
  vulnerability publicly before then.
- You are credited in the advisory if you ask to be.
- Fixes are announced in a
  [GitHub Security Advisory](https://github.com/veteranbv/inky-bird/security/advisories)
  and in the release notes.

## Supported versions

Security fixes go to the latest 1.x release and to `main`. Older releases are
not patched; upgrade to the latest 1.x release to get a fix.

## Trust boundaries

- Owner cookies are host-scoped, not port-scoped. Trust all services reachable
  at the owner hostname/IP, or use the [documented address or HTTPS identity
  isolation](docs/browser-access.md#sessions-and-private-files). Local HTTP does
  not isolate hostile services on the same host address or network.
- No self-hosted runner is registered for this repository. Every workflow
  job runs on a GitHub-hosted runner, and CI fails any workflow that names a
  self-hosted runner, a custom runner label or a runner group.
- Workflows on a pull request from an outside contributor wait for maintainer
  approval before they run, and then run only deterministic checks.
- No workflow deploys to a controller or display. The owner deploys from the
  controller itself.
- External pull requests do not receive Codex, notification, deployment, or publishing credentials.
- Catalog submissions are treated as untrusted data until deterministic checks
  and an independent review pass.
- Container images publish only from trusted `main`, a repository release, or
  an owner-started workflow. Pull requests can build images but cannot publish
  them.
- A release builds nothing new. Its version must match `pyproject.toml`, its
  commit must be on `main` with a passing `ci` run, and it re-tags the image
  `main` already published for that commit after the image runs natively on
  arm64. The release workflow never moves a published version tag to a
  different image or replaces an uploaded release file.
