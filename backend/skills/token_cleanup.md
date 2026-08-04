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

## Scoring Signals

Use these signals to build a cleanup score (0–100). Weight each signal based on how confidently the data supports revocation — safety is the priority here, so err toward lower scores when evidence is thin.

**Confirmed idleness over a long window:** A token with zero calls across a full 30-day observation window has been dormant long enough to rule out infrequent-but-real usage. This is the strongest cleanup signal and should carry the most weight.

**Redundant alongside an active sibling:** When a token is idle while a sibling with the same name carries meaningful traffic, the idle token is clearly the old credential from a completed migration. Its removal is safe.

**Short observation window:** A window under 30 days cannot confirm long-term idleness — sporadic integrations may not fire every day. Zero calls in a short window is weak evidence. Apply a strong penalty; prefer `insufficient_data` over `token_cleanup` when the window is too short.

**Actively used token:** Any token with a measurable call rate is not idle and must not be revoked. Score near zero.

**No Splunk data:** Without usage data, revocation cannot be safely recommended.

## Recommendation Format

Name this token by **ID** and **name**. State that it recorded zero calls over the full observation window (cite the window length in days). Confirm that at least one active sibling exists before recommending deletion.
