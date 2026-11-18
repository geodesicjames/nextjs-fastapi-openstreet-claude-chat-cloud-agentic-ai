# v1: name filter for search_places (#29, PR #30)

`search_places` gained a case- and accent-insensitive `name` filter, results are ordered by distance
before the 500-result cap, and the system prompt says to find a named place by name before looking up
its details. Measures whether this fixes the baseline's d02 failure (a named café outside the nearest
20) without side effects.

Result (26 cases × 2, Haiku 5.5): tool_use 52/52 (baseline 51/52; only d02 changed, 1/2 → 2/2).
`name` was used in all 8 runs that ask about a named place and in none of the 44 others. grounded
42/52 in both runs. The 7 cases that moved went both ways, none involved `name`, and the movement is
run-to-run noise. Cost $0.55.
