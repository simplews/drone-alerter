## What & why

## Checklist
- [ ] `ruff check .` and `pytest` pass; new behavior has tests
- [ ] No secrets, tokens, subscriber data, raw channel dumps or exact interception/impact locations
- [ ] Alert wording never says "all clear" / "target destroyed" (see `CLAUDE.md`)
- [ ] New config options added to `src/config.py` and `.env.example`
- [ ] New dependencies are justified, maintained and license-compatible
