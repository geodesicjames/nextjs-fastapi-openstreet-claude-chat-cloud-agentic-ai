# Chat tool-use eval: metrics

**Flow:** one user message → `chat.stream_reply` (real model, real tools) → graded.
**Data:** frozen central-Edinburgh fixture (`backend/evals/chat_tools/fixture.json.gz`,
1,368 places, © OpenStreetMap contributors). Overpass and Nominatim are stubbed (the
fixture's cells, and a 13-place gazetteer), so runs are reproducible.
**Cases:** 26, written by Claude and reviewed by the project owner (no real traffic yet):
search 14, details 3, save 3 (one where saving must *not* happen), no-tools 4, no-data 2.

## Metrics

| id | kind | how | passes when |
| --- | --- | --- | --- |
| `tool_use` (headline) | binary | code (`grade.py`) | the trajectory meets the case's expectations: search centred within 800 m (1 km for long streets) of the named place, right categories, radius cap, details/save on the right place, required/forbidden tools, or no tools at all. Properties, not an exact sequence. |
| `grounded` | binary | judge: `claude-sonnet-5-5` | no invented places, no claim about a returned place that the tool results don't support, and no hiding that data was missing. Derivable inferences (directions from coordinates, rounding, walking time), self-descriptions and general facts about well-known areas are allowed; guesses from names are not. Pass/fail is computed from per-claim verdicts, not one overall yes/no. |

Side columns: latency (s), tool calls, cost (app model + judge, derived from usage), refused /
truncated (none in the baseline).

## Baseline: Haiku 5.5, effort low (2026-11-18)

26 cases × 2 reps = 52 runs, 0 errors, 0 truncated.

| metric | score | 95% CI |
| --- | --- | --- |
| tool_use | 51/52 = 98% | 90–100% |
| grounded | 42/52 = 81% | 68–89% |

Median latency 4.1 s (max 14.4 s), 2.0 tool calls per question, about $0.0005 of Haiku per
question. The judge costs about $0.01 per question (20× the app), so a full 52-run pass is
about $0.55, almost all of it the judge.

## v1: name filter for search_places (2026-11-18)

| metric | baseline | v1 |
| --- | --- | --- |
| tool_use | 51/52 = 98% | **52/52 = 100%** (CI 93–100%) |
| grounded | 42/52 = 81% | 42/52 = 81% |

Only d02 changed on `tool_use` (1/2 → 2/2). `name` was used in all 8 named-place runs and in none of
the 44 others. On `grounded`, 7 cases moved in both directions, none involving `name`. That is the
first direct measurement of run-to-run noise: **about a quarter of cases can flip between runs**, so
`grounded` only detects large changes at this size. Median latency 3.9 s; the run cost $0.55.

## Known limitations

- **Judge noise isn't measured yet.** The final grade came from two re-grades that changed the
  rubric and then the output schema, so flips between them mix rubric changes with noise.
  To measure it: `--regrade` twice with no changes in between and count the flips.
- **One known false pass:** s04 rep 0 says the named viewpoints are "the closest", but the
  results have nearer unnamed ones (47 m, 97 m). Read `grounded` as roughly 79–81%.
- **26 cases × 2 reps** resolves differences of roughly ±14 points on a pass rate. That's enough
  to catch large regressions, not to rank close variants. Add cases or reps before
  hill-climbing on small changes.
- **Cases are synthetic.** Replace or extend them with real questions once there is traffic.

## Findings worth acting on

- ~~**No name search**~~: fixed in v1 (#29). d02 now passes on both reps.
- **Embellishment** is the main `grounded` failure: guesses from names ("inside the park"),
  loose landmark references ("near Costa"), miscounts, and adjectives like "quiet" or "large".
  The system prompt could address these, and this eval can measure whether it helps.
