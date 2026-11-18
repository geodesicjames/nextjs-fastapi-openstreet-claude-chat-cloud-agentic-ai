# v3: counts from total_found, read tags literally (#34, PR #35)

Adds two pieces of grounding guidance on top of v2: get counts from `total_found` or say "several" (never
count a list by hand), and read tags literally. Targets v2's two new failure types.

Result (26 cases × 4, Haiku 5.5): grounded 90/104 = 87% against v2's 91/104 = 88%. Paired by case:
−1.0 points (95% CI −11.5 to +10.6), so no measurable overall change. The targeted failures were fixed:
s02 (tag over-read) 2/4 → 4/4, and s04 (wrong count) 1/4 → 3/4; it now lists the viewpoints instead of
counting them. Other cases moved down (s07 1.00 → 0.25, s10 and s11 1.00 → 0.50). In s07 the answer
calls a list "the nearest eight" while skipping closer cafés. Possibly induced by "say when you list
only some results", possibly noise; this eval can't tell which at this size. tool_use 104/104; answer
length unchanged. Cost $1.03.
