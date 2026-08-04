# Skill: Rate Limit Pressure

Load this skill when `rate_429 > 0` for any token. 429s confirm that rate limiting is occurring — but fill percentage relative to the rate limit determines whether it is a burst spike or a structural problem requiring action.

## Core Principle

A token at 12% fill on nonpro_1x1 with 429s is hitting burst spikes — the average load is healthy, so rotation or audit won't fix it. A token at 88% fill with 429s is running near its sustained limit — any traffic spike will continuously trigger 429s.

**Fill % is the primary signal. 429 count confirms the problem exists, not how bad it is.**

## Tier Reference (fill_pct = calls/s ÷ leak_rate × 100)

The store's exact tier is unknown. Use nonpro_1x1 as the conservative baseline — if the store is on a pro plan, the same fill % represents less actual pressure, but the thresholds below are calibrated for the worst-case (cheapest) tier.

| nonpro_1x1 fill % | Interpretation | Recommended action |
|-------------------|----------------|--------------------|
| < 30%             | Burst-only. Average load is well within capacity. 429s are traffic spikes, not sustained over-limit usage. | no_action |
| 30–79%            | Moderate to high sustained load. The token regularly approaches its ceiling; 429s will recur with normal traffic variation. | token_rotation (redistribute load) |
| ≥ 80%             | At or above sustained capacity. 429s are continuous, not isolated spikes. The integration is structurally over-limit. | security_audit |

If fill_pct ≥ 50% even on pro_10x3 (the most generous tier), recommend security_audit — the token exceeds any plan's sustained capacity.

## Scoring Signals

Use these signals to build a rate_limit_pressure score (0–100). The score reflects how urgently the 429 pattern demands action.

**Sustained over-capacity with 429s:** When fill % is at or above the sustained limit ceiling AND 429s are present, the token is structurally over-limit — 429s are not accidental. This demands strong action and should score very high.

**High fill % with 429s:** A token approaching its ceiling (well above half of sustained capacity) with observed 429s will recurrently trigger rate limiting under normal traffic variation. Score high — the situation will worsen without intervention.

**Low fill % with 429s:** A token well within its average capacity that still shows 429s is hitting burst spikes, not sustained over-limit usage. The average load is healthy; the 429s reflect temporary bursts that the bucket absorbs. No structural action is needed — score near zero.

**Exceeds all plan tiers with 429s:** A token that exceeds sustained capacity even on the most generous available plan has a structural usage problem regardless of the store's actual plan. Score at maximum.

**No 429s observed:** This skill should not have been loaded — 429s are the prerequisite for this analysis. Score near zero.

## Recommendation Format

State: (1) fill_pct for the relevant tier, (2) the 429 count observed in the window, (3) burst vs. sustained interpretation, and (4) the recommended action with rationale. Do not recommend rotation or security_audit for burst-only pressure (fill_pct < 30% for nonpro_1x1).
