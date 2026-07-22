# Skill: Token Rotation

Recommend rotation for a specific token when it is actively handling significant traffic and its credentials should be refreshed, or when traffic is unbalanced between it and a newer sibling.

## When to Recommend Rotation for a Token

- This token's fill_pct is a meaningful fraction of the store's rate limit capacity — active credentials deserve periodic rotation
- This token and a sibling share the same name, but this token is older yet still carries most of the traffic (incomplete migration)
- This token is the only one for the store and carries >80% of total store traffic — rotation adds resilience

## Scoring Signals for each individual token (apply all that match, cap at 100)

| Signal | Points |
|--------|--------|
| This token received any HTTP 429 responses (rate limiting occurring — traffic must be redistributed) | +45 |
| This token received > 100 HTTP 429 responses in the window (severe rate limiting) | +15 additional |
| This token's fill_pct ≥ 50% for nonpro_1x1 (avg ≥ 1.0 calls/s) | +35 |
| This token's fill_pct ≥ 25% for nonpro_2x1 or pro_2x2 | +30 |
| This token's fill_pct ≥ 25% for pro_5x3 | +25 |
| This token's fill_pct ≥ 10% for pro_10x3 | +20 |
| This token shares a name with a newer token; this token still carries more traffic (migration stalled) | +25 |
| This token accounts for >80% of total store calls/s and no sibling exists | +20 |
| This token's fill_pct < 2% for nonpro_1x1 AND no 429s (nearly idle, no load pressure) | -25 |
| No Splunk data for this token | -20 |

## Recommendation Format

Name this token by **ID** and **name**, state its exact fill_pct for the relevant tier, and explain the rotation rationale (high-traffic path, stalled migration, sole token for the store, etc.). If 429 errors were observed, explicitly recommend: (1) rotating more tokens to distribute load, or (2) adding delay between requests, or both — depending on whether multiple siblings exist.
