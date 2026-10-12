# Adapt the system to another city

The engine (tracker, normalizer, ETA, learning) does not depend on one city. All data that is specific to the protected city is in one **city profile**: a JSON file in `data/profiles/`.

Shipped profiles:
- `ivano-frankivsk.json`: the production profile.
- `example-city.json`: a small fictional profile. Copy it as a starting point.

## Steps

1. **Copy a profile.** `cp data/profiles/example-city.json data/profiles/<your-city>.json`. The file name must be lowercase kebab-case, and the `id` field must be the same as the file name.
2. **Define the corridor.** In `points`, list the settlements on the approach path. For each point set:
   - `id`: unique ASCII identifier.
   - `names`: every grammatical case and spelling that channels use (lowercase). The normalizer matches these.
   - `level`: `1` (approach), `2` (close), `3` (immediate danger).
   - `distance_km`: flight distance to the protected city.
   - `eta_min` and `eta_max` (optional): ETA range in minutes. If you omit them, the system calculates a range from `distance_km` and `speed_kmh`.
3. **Set the keywords.** `target_keywords` select the posts about the drone type. `cancel_keywords` mark shoot-down, loss and turn-away. `stopwords` are words that look like place names but are not (regions, directions).
4. **Set the user texts.** The `texts` block holds the city name in the grammatical cases that the bot messages need, the route description, and the corridor table for `/howitworks`. These texts are in Ukrainian.
5. **Set the alert region.** `siren_region` must be the region name in the air-raid alert API.
6. **Select the profile.** Set `CITY_PROFILE=<your-city>` in `.env`. Set `MONITORED_CHANNELS` to the channels that cover your corridor.
7. **Start with a clean reference.** The reference (`data/reference/`) and the learning database (`data/learning.db`) belong to one city. Use new paths (`REFERENCE_DIR`, `LEARNING_DB_PATH`) or empty directories for a new city. The first start seeds `v0001` from the profile.
8. **Validate before you trust it.** Run `pytest`. Replay historical posts with `python -m src.engine.shadow_runner` and check the Go/No-Go criteria in `docs/PRD.md`. Label real episodes (`python -m src.learning.cli label`) and run `retrain`. With enough data the ETA ranges calibrate themselves.

## Profile fields

| Field | Meaning |
|---|---|
| `id` | Profile name. Same as the file name. |
| `short_name` | Short code for the API title and the HTTP user agent. |
| `siren_region` | Region name in the air-raid alert API. |
| `speed_kmh` | `[min, max]` drone speed for the physical ETA estimate. |
| `post_lag_min` | `[min, max]` delay between the event and the channel post. |
| `texts` | City and route names for the bot messages (see the example profile). |
| `target_keywords`, `cancel_keywords`, `stopwords` | Lowercase keyword lists. |
| `points` | The corridor points (see step 2). |

The loader checks the profile at start and rejects it with a clear error when a field is missing, a point `id` is duplicated, a `level` is not 1, 2 or 3, or an ETA range is invalid.

## Rules for a new deployment

- All rules in `CLAUDE.md` apply to every city. The alert text must never say "all clear" or "target destroyed". It shows an interval, not an exact minute. The system does not replace the official air-raid alert.
- Do not publish or store shoot-down coordinates, air-defense activity or exact impact places.
- Use an invented or anonymized corridor in public examples and tests. Do not publish real operational data.
- Do not commit your channel dumps, `*.db` files or secrets. See `SECURITY.md`.

## Migration note

The outcome labels no longer contain the city code. `reached_if` is now `reached_target`, and `downed_en_route_if` is now `downed_en_route`. An existing `learning.db` is migrated automatically at the next start.
