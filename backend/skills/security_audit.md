# Skill: Security Audit

Recommend a security audit for a specific token when its usage pattern indicates sustained rate limit pressure, capacity risk, or an anomalous call rate that warrants investigation.

## When to Recommend a Security Audit for a Token

- This token's fill_pct exceeds 100% for any tier — its average call rate equals or exceeds sustained capacity (rate limiting is occurring on this token)
- This token's call rate is so high it likely triggers rate limiting for the entire store
- This token shows an unexpectedly large spike relative to sibling tokens — potential runaway integration

## Scoring Signals

Use these signals to build a security_audit score (0–100). Weight each signal based on the severity of the capacity pressure the data shows.

**At or beyond sustained capacity:** A token whose average call rate equals or exceeds the store's sustained limit is in continuous rate-limiting territory — not occasional spikes, but structural over-limit usage. This is the most urgent signal and should carry the most weight. The severity increases with the fill % and is highest when the token exceeds limits even on the most generous plan tier.

**Approaching capacity ceiling:** A token running at a high fraction of its sustained limit (not yet at 100% but clearly trending there) is at risk during any traffic spike. The closer to the ceiling, the stronger the audit signal.

**Low or no capacity pressure:** A token well under its sustained limit shows no structural risk. Score near zero — an audit would find nothing actionable.

**Anomalous spike relative to siblings:** A token that shows dramatically higher call rates than other tokens for the same store may indicate a runaway integration or unauthorized use. Treat disparity as an audit signal proportional to how extreme it is.

**No Splunk data:** Without usage data, capacity risk cannot be assessed.

**Note:** 429 signals are handled by the `rate_limit_pressure` skill, not here. Load that skill alongside this one when `rate_429 > 0` — it determines whether 429s indicate burst or sustained over-limit usage, and routes to security_audit when fill_pct is high.

## Recommendation Format

Name this token by **ID** and **name**. State its exact calls/s and fill_pct for the relevant tier. Classify severity (critical / high / medium) based on how far fill_pct exceeds 80%. Recommend immediate investigation of the integration using this token.
