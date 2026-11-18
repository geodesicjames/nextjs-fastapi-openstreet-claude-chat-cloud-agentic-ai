# v2: system prompt says to describe places only with what the tools returned (#32, PR #33)

One prompt bullet against embellishment: no guesses from names, no impressions, no positions relative
to landmarks the results don't mention, and no unchecked counts. Its examples deliberately don't come
from the eval's cases or judge checks.

Result (26 cases × 4, Haiku 5.5): grounded 91/104 = 88% (CI 80–93%) against 81% before (baseline + v1,
104 runs). Paired by case: +6.7 points, but the 95% bootstrap CI is −4.8 to +18.3, so the gain is
likely but not conclusive. 8 cases improved (the worst offenders s11, s12 and v03 among them) and 3 got
worse: s04 now states an exact count of unnamed viewpoints and gets it wrong (4 instead of 5), and s02
misreads the tag `female=yes` as "female-only". tool_use 104/104; answer length unchanged (median 80
words). Cost $1.05.
