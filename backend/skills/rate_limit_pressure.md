# Skill: Rate Limit Pressure

Load this skill when `rate_429 > 0` for any token. 429s confirm that rate limiting is occurring — but fill percentage relative to the rate limit determines whether it is a burst spike or a structural problem requiring action.

## Core Principle

A token at 12% fill on nonpro_1x1 with 429s is hitting burst spikes — the average load is healthy, so rotation or audit won't fix it. A token at 88% fill with 429s is running near its sustained limit — any traffic spike will continuously trigger 429s.

**Fill % is the primary signal. 429 count confirms the problem exists, not how bad it is.**

## Tier Reference (fill_pct = calls/s ÷ leak_rate × 100)

**Use the store's actual rate limit when available.** The usage summary includes an "ACTUAL store rate limit" line derived from `rate_limit_multiplier` in `store.general_attributes` (leak_rate = multiplier × 2 calls/s). Always use that fill % for your analysis — do not default to nonpro_1x1 if the actual tier is known.

If store settings are unavailable, use nonpro_1x1 (2/s) as the conservative baseline only as a last resort, and say so explicitly.

| Actual-tier fill % | Interpretation | Recommended action |
|--------------------|----------------|--------------------|
| < 30%              | Burst-only. Average load is well within capacity. 429s are traffic spikes, not sustained over-limit usage. | no_action |
| 30–79%             | Moderate to high sustained load. The token regularly approaches its ceiling; 429s will recur with normal traffic variation. | token_rotation (redistribute load) |
| ≥ 80%              | At or above sustained capacity. 429s are continuous, not isolated spikes. The integration is structurally over-limit. | security_audit |

Additionally: if the 429 count is > 20% of total calls in the window, note this explicitly — even at low fill %, a high 429 ratio signals a burst or integration problem worth flagging.

If fill_pct ≥ 50% even on pro_10x3 (the most generous tier), recommend security_audit — the token exceeds any plan's sustained capacity.

## Scoring Signals

Score each dimension on 0–100. Use both fill % (sustained load) AND the 429 ratio (429_count ÷ total_calls) as independent signals. Either alone can justify action.

### security_audit_score

| Condition | Score range |
|-----------|-------------|
| fill % ≥ 80% on actual tier AND 429s present | 85–100 |
| fill % 30–79% on actual tier AND 429s present | 60–80 |
| 429 ratio > 30% of total calls (regardless of fill %) | 70–90 — high failure rate signals a structural burst or integration problem even if average load looks low |
| 429 ratio 10–30% of total calls | 40–65 |
| fill % < 30% AND 429 ratio < 10% | 0–15 — true burst spikes, no structural issue |
| No 429s | 0 |

### rotation_score

| Condition | Score range |
|-----------|-------------|
| fill % 30–79% on actual tier AND 429s present | 55–75 — redistributing load across tokens would reduce per-token pressure |
| fill % ≥ 80% on actual tier AND 429s present | 40–60 — rotation alone insufficient; pair with security_audit |
| 429 ratio > 30% with low fill % | 20–40 — burst may indicate a single caller that should be split off |
| fill % < 30% AND 429 ratio < 10% | 0–10 |

### cleanup_score

Always near 0 for tokens with active 429s — an actively rate-limited token is not idle.

### Decision rule

Score the dimension with the strongest signal highest. The recommended action is whichever dimension scores ≥ 20 with the highest value. A 429 ratio > 30% must produce at least one dimension ≥ 50.

## Recommendation Format

State: (1) fill_pct for the relevant tier, (2) the 429 count observed in the window, (3) burst vs. sustained interpretation, and (4) the recommended action with rationale. Do not recommend rotation or security_audit for burst-only pressure (fill_pct < 30% for nonpro_1x1).
