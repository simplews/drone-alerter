# AGENTS.md — drone-alert

This file is for AI assistants and IDEs (Cursor, Codex, Copilot, Windsurf, Gemini and others). It follows ASD-STE100 (Simplified Technical English).
`CLAUDE.md` has the full version. If the two files disagree, follow `CLAUDE.md`.

## Project
drone-alert is an early-warning system for jet-powered drones that fly toward Ivano-Frankivsk. It reads public Telegram channels (Telethon). It finds place names (`src/nlp/`). It calculates the ETA (`src/engine/`). It sends alerts with a bot (aiogram, `src/telegram/`).
Stack: Python 3.11, FastAPI, SQLAlchemy.
The engine is city-agnostic. All city-specific data (corridor points, ETA seed, keywords, bot texts, alert region) is in `data/profiles/<city>.json`, loaded by `src/profile.py`. Do not hard-code a city or corridor point in `src/`. See `docs/ADAPTING.md`.
Bot messages to users are in Ukrainian. Do not translate them. Write new code, comments, docs and commit messages in English.
More information: `README.md`, `docs/PRD.md`, `docs/LEARNING.md`.

## Commands
- `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- `pytest` and `ruff check .` must pass before each commit.
- `python -m src.main` starts the service. Make `.env` from `.env.example` first.

## Git flow
- `master` is the stable branch. Do not commit to it directly. Do not force-push to it. Do not merge without a PR.
- Make a new branch for each feature or fix. Start from an up-to-date `master`. Use these prefixes: `feature/`, `fix/`, `hotfix/`, `docs/`, `chore/`, `refactor/`, `test/`. Add a kebab-case description (`feature/eta-calibration-alerts`).
- One branch holds one logical change. Do not mix tasks.
- Use Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`. Make each commit small and logical.
- Open a PR against `master`. Use `.github/pull_request_template.md`. CI must pass. Merge with squash or rebase. Delete the branch after the merge.
- Do not commit, push or merge without a direct instruction from the user.
  - Allowed: create a branch, commit to it, push that branch.
  - Not allowed: push to `master`, merge, rewrite the history of another person.

## Security (never break these)
- Put secrets only in `.env` or environment variables (Git ignores `.env`). Do not read, print or commit tokens, `TELETHON_STRING_SESSION`, API hashes or subscriber `chat_id` values. In tests, use clear fake values.
- Do not commit `*.db`, `*.session`, `data/attacks_*.json`, `data/historical_*.json`, channel dumps or subscriber data.
- When you add a configuration option, add it to `src/config.py` and `.env.example`. Do not put a real value in `.env.example`.
- Do not use `eval`, `exec`, `pickle` or `shell=True`. Use bound parameters for all SQL.
- Treat all Telegram text as untrusted. Limit its length. Escape it in output.
- Do not log secrets or full post texts. The system deletes raw posts after 14 days.
- Add only necessary dependencies. They must have a maintainer and a compatible license.
- Reference files `data/reference/vNNNN.json` are immutable. Create a new version with `python -m src.learning.cli retrain`. Do not edit an existing version.
- If a secret enters a commit, assume that it is compromised. Revoke it first. Then clean the history.
- Report vulnerabilities as described in `SECURITY.md`. Do not use public issues.

## Alert rules
- Never write "all clear" ("відбій"), "safe" ("безпечно") or "target destroyed" ("ціль знищено"). Write only: "імовірно уражена, залишайтеся в укриттях до офіційного відбою".
- Do not publish or store the coordinates of shoot-downs, air-defense activity, or exact impact places.
- Show the user an ETA interval. Do not show an exact minute. The system does not replace the official air-raid alert.

## Code style
- Write code in the style of the surrounding code: same comment density, same naming, same idioms.
- Add a test in `tests/` for each new behavior (parsing, ETA, tracker).
- Do not add a license or a large dependency without the owner's approval.
