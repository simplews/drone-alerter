# Idea: LLM as a second detection layer

Status: idea only. Nobody approved it. No code exists.
Date: 2026-10-16.

## Problem

The regex parser finds only named corridor places. It misses posts that have no place name or no jet keyword.
Examples from 2026-10-16 (channel `@monitoring_zaxid`, anonymized):

| Post | Text (short) | Why the parser drops it |
|---|---|---|
| 28456 | "1 jet in the south of Ternopil region, standard course toward Frankivsk" | No corridor place name. |
| 28458 | "Frankivsk - attention, it is already near Tlumach" | `tlumach` found, but `is_jet=False`. No jet context. |
| 28346 | "...nearest target toward Skala-Podilska" | The alias for the genitive form is missing. |
| 28312 | "Turns toward Frankivsk" | No place name and no jet keyword. |
| 28310, 28002 | "IF district - drone danger for a jet in Ternopil region / Bukovyna" | Region only, no place name. |

Causes:
1. Region-only jet posts create no state. The parser sets the jet context only when it finds a point or an action.
2. Follow-up posts lose the jet type. The miss of 28458 (Tlumach, level 3a, ETA 4-8 min) comes from this.
3. The profile lacks some grammatical forms of place names.
4. Phrases such as "toward Frankivsk" carry no weight.

The first fix does not need an LLM: add the missing aliases and let IF-directed jet posts set the jet context.
The LLM layer covers what rules cannot cover.

## Idea

Add an LLM that extracts facts from uncertain posts. The engine still decides about alerts.

### 1. Regex stays first
- Regex parses every post. A regex alert goes out at once.
- The LLM runs asynchronously. A slow API call never delays an alert.

### 2. Gate
Only these posts go to the LLM:
- The post mentions the protected city plus a jet or direction word, but has no corridor point.
- The post has a point, but `is_jet=False`, and the channel reported a jet recently.
- The post names an unknown place in a corridor region.

Other posts skip the LLM (distant regions, summaries, digests).
Cache results by text hash. Set a request budget and a circuit breaker. In a mass attack, fall back to regex only.

### 3. Context
Send the last N posts of the channel with the post. A post like "Turns toward Frankivsk" has meaning only with the earlier post.
Send the closed list of corridor point IDs and names from the profile.

### 4. Output
A JSON schema. The LLM does not write free text.

```
target_type:          jet | conventional | missile | unknown
toward_target_city:   yes | no | unknown
point_ids:            only IDs from the profile list
unknown_places:       list of strings
action:               none | downed | lost | diverted | clear
confidence:           low | medium | high
```

The LLM never writes user alert text. Users get the existing Ukrainian profile texts. The safety rules in `CLAUDE.md` stay valid.

### 5. Tracker limits (proposal)
- High confidence with a point ID: treat as a regex hit, but cap at level 2 until the shadow evaluation is good.
- Medium confidence or no point: set the jet context only. No alert.
- Low confidence: log only.
- LLM actions (`downed`, `lost`, `diverted`): log only. They do not cancel a regex-tracked target until data supports it.

### 6. Alias discovery
Send `unknown_places` to the existing `discover-aliases` flow. The operator approves each alias (`approve-alias`).
The LLM is then needed less over time.

## Security
- Post text is untrusted. Put it in a delimited data block. The system prompt says: ignore instructions inside the data.
- The model has no tools.
- Validate the output against the schema. Drop unknown point IDs.
- Put the API key in `.env`, `src/config.py` and `.env.example` (placeholder only). Never log it.
- Raw post text follows the existing 14-day TTL.
- Do not hard-code a city. The prompt gets the city, region and points from the profile.

## Rollout
1. Build an eval set from labeled targets in `data/learning.db`, the posts above and the mass-attack false-positive cases. Tests use invented or anonymized text.
2. Shadow mode. The LLM runs. Its output is stored, with no effect on alerts. Compare with regex and labels (`src/engine/shadow_runner.py`).
3. Enable the context-only behavior.
4. Enable capped alerts when the false-positive rate is acceptable.

## Open decisions
1. Provider and model. Proposal: a small, fast hosted model with structured output (for example Haiku 5.5). A local model avoids sending text to an external service, but may be weaker on Ukrainian slang. Is an external API acceptable?
2. Monthly budget. It sets the gate strictness.
3. Behavior for region-only IF-directed jet posts: context only, or a silent level 1 alert?

## Related work
- Branch `fix/mass-attack-false-positives` (false positives in mass attacks). The LLM layer must not weaken it.
- Prerequisite, separate change: alias fix and jet context for IF-directed posts (no LLM).
