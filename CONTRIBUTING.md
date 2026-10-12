# Contributing

1. Fork, create a feature branch, open a PR against `main`.
2. Set up: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
3. Before pushing: `ruff check .` and `pytest` must pass. Add tests for new behavior.
4. Use Conventional Commits (`feat:`, `fix:`, `docs:` ...).
5. Never include secrets, real subscriber data, raw channel dumps or exact interception/impact locations in code, tests, docs or commit messages.
6. Alert wording must never say "all clear" / "target destroyed" — see "Безпекові правила" in `CLAUDE.md`.
7. Security issues: follow `SECURITY.md`, not public issues.
