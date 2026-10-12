# Security Policy

## Supported Versions

Only the latest commit on `main` is supported with security fixes.

## Reporting a Vulnerability

**Do not open a public issue** for security problems — this system handles Telegram credentials and alert-subscriber data.

Report privately via GitHub's "Report a vulnerability" (Security → Advisories) on this repository, or by email to the maintainer listed on the GitHub profile.

Please include a description, reproduction steps and impact. You can expect an acknowledgement within 7 days and a status update at least every 14 days until resolved.

## Scope and handling of secrets

- Never commit `.env`, Telegram sessions (`TELETHON_STRING_SESSION`), bot tokens or `*.db` files.
- If you find a leaked secret in the repository or its history, report it privately; it will be revoked and rotated before any history cleanup.
- Out of scope: the accuracy of ETA estimates. The system is advisory and does **not** replace official air-raid alerts.
