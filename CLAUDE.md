# CLAUDE.md — drone-alert

This file follows ASD-STE100 (Simplified Technical English). Keep new text in the same style: short sentences, active voice, one instruction per sentence.

## Project
drone-alert is an early-warning system for jet-powered drones ("jet Geran" / Shahed-238) that fly toward Ivano-Frankivsk (IF).
The system reads public Telegram monitoring channels. It finds the names of places in the flight corridor. It calculates the estimated time of arrival (ETA) at Khryplyn airfield. It sends alerts through a bot.

- Text that the bot sends to users is in Ukrainian. Do not translate it.
- Existing code comments are in Ukrainian. Write new code, comments, docs and commit messages in English.
- The engine is city-agnostic. Do not hard-code a city, region or corridor point in `src/`. Put it in the profile.
- More information: `README.md` (architecture), `docs/PRD.md` (requirements), `docs/LEARNING.md` (learning), `docs/DEPLOYMENT.md`, `docs/ADAPTING.md` (other cities).

## Commands
- Environment: `.venv` (Python). Dependencies are in `requirements.txt`. Run the CLI with `.venv/bin/python -m src.learning.cli ...`.
- Test and lint: `pytest`, `ruff check .`.
- Service: `python -m src.main`. A `.env` file is necessary. Use `.env.example` as the template.
- Docker: `docker compose up -d`. Check the service with `curl localhost:8080/health`.
- ETA reference CLI: `seed`, `status`, `show`, `unlabeled`, `label`, `discover-aliases`, `aliases`, `approve-alias`, `reject-alias`, `retrain`, `diff N`, `promote N`, `rollback`, `purge-posts --days 14`.

## Structure
- `src/telegram/`: Telethon listener (`listener.py`), aiogram bot (`bot.py`), session generator, `export_history.py`.
- `src/nlp/`: place-name normalization (grammatical cases, homonyms) and action types (`DOWNED`, `LOST`, `DIVERTED`).
- `src/profile.py` and `data/profiles/<city>.json`: the city profile. All city-specific data (corridor points, ETA seed, keywords, bot texts, alert region) lives there. The `CITY_PROFILE` setting selects it. See `docs/ADAPTING.md`.
- `src/engine/`: target tracker, ETA, simulation, `shadow_runner.py` (replay of historical episodes).
- `src/learning/`: versioned references `data/reference/vNNNN.json` (the file `ACTIVE` points to the active version), calibration, labels in `data/learning.db`.
- `src/siren/`: official air-raid alerts. `src/api/`: `/health` and `/targets`.
- `data/attacks_october.json`: raw channel posts. Git ignores this file. Do not commit it.

## Target labels (`label`)
Allowed results: `reached_target`, `downed_en_route`, `diverted`, `unknown`.
- `reached_target` needs `--arrived-at <ISO UTC>Z`. Do not use `--arrived-at` with other results.
- `downed_en_route`: the target flew toward IF and was shot down. `diverted`: the target flew to a different region.
- Arbitration rule: `arrived-at` is the first mention of Khryplyn or the airfield. A post that names Khryplyn means the target arrived. A post that names Tysmenytsia means the target is almost over the city, but it does not name Khryplyn (medium confidence).
- Target IDs have the format `T-YYYYMMDDHHMM-xxxx`. The time in the ID is UTC. Channels show UTC time. Kyiv time is UTC+3 until 25.10.2026.
- A dataset record is a time cluster of place names. One physical target can have more than one record ("twins"). Twins get the same label. Mark this assumption in the record.
- Do not set a label without evidence. Keep the target in `unlabeled`.
- Sources for verification: `@monitoring_zaxid` (main), `@kudy_letyt`, `@monitorwarr`, `@kpszsu`. The channel edits posts (for example "updated: збили!!"). Read the last edit.

## Corridor and ETA (reference)
Nova/Stara Ushytsia, Dunaivtsi → Kamianets-Podilskyi → Skala-Podilska / Borshchiv → Tovste → Chernelytsia → Tlumach → Tysmenytsia → Khryplyn.
Observed time to the airfield from the first mention: Nova Ushytsia 25–33 min, Borshchiv 14–21, Tovste 15–17, Tlumach 4–8.
For a small sample (n<8), new observations only widen the physical interval. Narrow the reference by 3 minutes at most for each update.

## Safety rules (never break these)
- Do not publish or store the coordinates of shoot-downs, air-defense activity, or exact impact places.
- Never write "all clear" ("відбій"), "safe" ("безпечно") or "target destroyed" ("ціль знищено"). Write only: "імовірно уражена, залишайтеся в укриттях до офіційного відбою" (probably hit, stay in shelter until the official all-clear).
- Show the user a time interval. Do not show an exact minute. The system does not replace the official air-raid alert.
- The system deletes raw post texts after 14 days (TTL). Do not log tokens.
- `.env` holds secrets (Telegram API, bot token, session). Do not read it. Do not print it. Do not commit it.

## Open-source conventions (mandatory)
The repository is public or can become public. Everyone can see each commit. Git history is permanent.

### Secrets and data
- Put secrets only in environment variables or `.env` (Git ignores `.env`). Never put tokens, `TELETHON_STRING_SESSION`, API hashes or subscriber `chat_id` values in code, tests, fixtures, docs, logs or commit messages. In tests, use clear fake values (for example `123456:TEST`).
- When you add a configuration option, add it to `src/config.py` and `.env.example` in the same change. Add a new city-specific value to the profile, not to `src/config.py`. Do not put a real value in `.env.example`.
- Do not commit `*.db`, `*.session`, `data/attacks_*.json`, `data/historical_*.json`, channel dumps or personal data of subscribers. Keep `subscribers.db` local or in the container volume.
- If a secret enters a commit, assume that it is compromised. Revoke it and issue a new one first (BotFather, my.telegram.org, `generate_session`). Then clean the history.
- Reference files `data/reference/vNNNN.json` are immutable. Do not edit an existing version. Create a new version with `retrain` or `promote`.

### Code security
- Do not use `eval`, `exec`, `pickle` or `shell=True`.
- Use SQLAlchemy with bound parameters for all SQL. Do not build SQL with f-strings.
- Treat all input from Telegram (post texts, bot commands, names) as untrusted. Limit its length. Do not run it as code or markup. Escape HTML and Markdown in output.
- Only the operator can use state-changing bot commands and the CLI operations `promote`, `rollback` and `purge-posts`.
- Public API endpoints are read-only. They do not return secrets or subscriber data (`/health` and `/targets` do not return `chat_id`).
- Do not log `.env` content, tokens, or full post texts older than the TTL.
- Add only necessary dependencies. Check the maintainer and the license. Set a lower version bound in `requirements.txt`. Watch for typosquatting.
- Docker: do not run as root. Do not copy `.env`, `.git` or `tests` into the image (use `.dockerignore`). Expose the API port only through a reverse proxy or a firewall.

### Development practice
- Before each commit, `ruff check .` and `pytest` must pass. Add a test for each new behavior (parsing, ETA, tracker).
- Use Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. Make one logical change for each commit.
- Do not use `git add -A` without a check. Read `git status` and `git diff --cached` for foreign files and secrets.
- Do not add sensitive operational information to the open code (see "Safety rules"). Examples in tests and docs must be invented or anonymized.
- A change to the alert behavior that touches the safety rules needs an explicit description in the PR.
- The owner chooses the project license (`LICENSE` does not exist yet). Do not choose one without the owner.

## Git flow (mandatory)
- `master` is the stable branch. Production deploys from it. Do not commit to it directly. Do not force-push to it. Change it only through a PR with green CI.
- Make a new branch for each feature, fix or change. Start from an up-to-date `master`:
  `git switch master && git pull --ff-only && git switch -c <type>/<short-description>`
- Branch prefixes: `feature/` (new function), `fix/` (bug), `hotfix/` (urgent production fix), `docs/`, `chore/` (infrastructure, dependencies, CI), `refactor/`, `test/`. Write the name in lowercase kebab-case, for example `feature/eta-calibration-alerts`.
- One branch holds one logical change. Do not mix different tasks in one branch or PR. Do not start new work in the branch of another feature.
- Before you open a PR, run `git fetch && git rebase origin/master`. After the PR is open, do not rewrite the history of the branch without a reason.
- Open the PR against `master`. Fill in the PR template. CI must pass. Get at least one review (a single maintainer does a self-check with the checklist). Merge with squash or rebase. Delete the branch after the merge.
- Make a `hotfix/` branch from `master`. Merge it into `master` as soon as CI passes. Put the release tag `vX.Y.Z` (SemVer) on `master`.
- Do not commit, push or merge without a direct instruction from the owner. An AI agent can create a branch, commit to it, and push that branch. An AI agent must not push to `master` and must not merge.

## Known issues
- Links in `README.md` point to an old path `/Users/.../antigravity/kind-bose/`. The links are obsolete.
- The danger levels in `README.md` and `docs/PRD.md` differ in some places (Tovste, Chernelytsia). Verify them against `src/engine/eta.py`.
- `SECURITY.md` is the vulnerability-reporting policy. The owner must add the contact.
