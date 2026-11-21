# Security

Please report a security problem privately, not in a public issue: use **Report a vulnerability** on this
repository's Security tab (GitHub's private vulnerability reporting). Say what you found, how to reproduce it, and
what it lets someone do. You will get an answer within a few days.

What matters most here:

- **Tokens.** An app signs a short-lived HS256 token for one workspace (`docs/integration.md`). A way to act for
  another workspace, to stretch a token past its expiry, or to use a `draft` token for `delete` is a vulnerability.
- **The app's endpoints.** The bot calls an app only at its pack's `api_base`, on the same origin as the app's
  registry `site`. A way to make it call anywhere else (SSRF) is a vulnerability.
- **Files.** Uploads and signed file links (`/v1/files/...`) must not let one workspace read another's files.
- **Publishing.** The bot has no tool that publishes; a way to make it post for a user is a vulnerability.

Secrets never belong in this repository: every key is an environment variable (`.env.example`).
