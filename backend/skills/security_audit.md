# Skill: Security Audit

Recommend a security audit for a specific token when its usage pattern indicates rate limit pressure, capacity risk, or an anomalous call rate that warrants investigation.

## When to Recommend a Security Audit for a Token

- This token's fill_pct exceeds 100% for any tier — its average call rate equals or exceeds sustained capacity (rate limiting is occurring on this token)
- This token's call rate is so high it likely triggers rate limiting for the entire store
- This token shows an unexpectedly large spike relative to sibling tokens — potential runaway integration

## Scoring Signals for each individual token (apply all that match, cap at 100)

| Signal | Points |
|--------|--------|
| This token received > 100 HTTP 429 responses in the window (confirmed severe rate limiting) | +40 |
| This token received any HTTP 429 responses (rate limiting confirmed) | +25 |
| This token fill_pct ≥ 100% for nonpro_1x1 (avg ≥ 2.0 calls/s) — rate limiting at baseline tier | +50 |
| This token fill_pct ≥ 100% for nonpro_2x1 (avg ≥ 4.0 calls/s) | +40 |
| This token fill_pct ≥ 100% for pro_5x3 (avg ≥ 10.0 calls/s) | +35 |
| This token's calls/s is ≥ 8.0 (fill_pct ≥ 80% for pro_10x3) — approaching hard limits | +30 |
| This token's calls/s is ≥ 5.0 (fill_pct ≥ 50% for pro_10x3) | +25 |
| This token fill_pct ≥ 80% for nonpro_2x1 (avg ≥ 3.2 calls/s) — approaching nonpro limit | +20 |
| This token fill_pct < 10% for nonpro_1x1 AND no 429s (no capacity pressure) | -20 |
| No Splunk data for this token | -25 |

## Recommendation Format

Name this token by **ID** and **name**. State its exact calls/s and fill_pct for the relevant tier. Classify severity (critical / high / medium) based on how far fill_pct exceeds 80%. Recommend immediate investigation of the integration using this token.
