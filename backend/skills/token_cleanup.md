# Skill: Token Cleanup

Recommend cleanup (revocation) for a specific token when it has shown zero usage over a meaningful observation window. Revocation must only be advised when the token has been completely idle for at least 30 days — never based on shorter windows.

## When to Recommend Cleanup for a Token

- This token recorded zero calls over a 30-day (or longer) window — definitively idle and safe to revoke
- This token's fill_pct is negligible across all tiers while a sibling token with the same name carries meaningful traffic — this token is redundant
- This token has zero calls and at least one other token with the same name is active

## When NOT to Recommend Cleanup

- The observation window is less than 30 days — zero calls in a short window is insufficient evidence for revocation; apply a heavy penalty
- Splunk data is unavailable for this token — cannot safely determine if it is truly unused
- This token still has calls/s > 0 — revocation is unsafe

## Scoring Signals for each individual token (apply all that match, cap at 100)

| Signal | Points |
|--------|--------|
| This token calls/s = 0.0 AND observation window ≥ 30 days | +50 |
| This token fill_pct < 1% for nonpro_1x1 AND window ≥ 30 days | +25 |
| This token is zero/near-zero while a sibling with the same name has fill_pct > 5% | +20 |
| 2+ tokens share this token's name; this token is the only one with no meaningful usage | +20 |
| This token calls/s > 0.04 AND fill_pct > 2% (actively used, revocation unsafe) | -35 |
| Observation window < 30 days (insufficient to confirm long-term idleness) | -40 |
| No Splunk data for this token | -30 |

## Recommendation Format

Name this token by **ID** and **name**. State that it recorded zero calls over the full observation window (cite the window length in days). Confirm that at least one active sibling exists before recommending deletion.
