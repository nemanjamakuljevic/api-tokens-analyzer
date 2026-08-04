# Skill: Token Rotation

Recommend rotation for a specific token when it is actively handling significant traffic and its credentials should be refreshed, or when traffic is unbalanced between it and a newer sibling.

## When to Recommend Rotation for a Token

- This token's fill_pct is a meaningful fraction of the store's rate limit capacity — active credentials deserve periodic rotation
- This token and a sibling share the same name, but this token is older yet still carries most of the traffic (incomplete migration)
- This token is the only one for the store and carries >80% of total store traffic — rotation adds resilience

## Scoring Signals

Use these signals to build a rotation score (0–100). Weight each signal based on how strongly the data supports it — signals that clearly apply should weigh more than signals with borderline evidence.

**High-load token:** A token consistently using a meaningful share of the store's rate limit capacity is under sustained pressure. The higher the fill %, the stronger the rotation signal — especially on the smallest (cheapest) tier, where headroom is most limited.

**Stalled migration:** When two tokens share the same name but the older one still carries most of the traffic, the migration to the new token is incomplete. This is one of the clearest rotation signals — credentials should rotate, and load should shift.

**Sole token:** A token that handles more than 80% of all store traffic with no sibling is a single point of failure. Rotation improves resilience even if load is moderate.

**Low or zero usage:** A nearly idle token has no active integration to protect. Rotation adds no value if the credentials aren't being used.

**No usage data:** Without Splunk data you cannot assess load or migration state. Score conservatively.

**Note:** If this token shows `rate_429 > 0`, also load the `rate_limit_pressure` skill. That skill determines whether 429s reflect burst spikes (no action needed) or sustained over-limit usage (rotation or audit warranted) based on fill %.

## Recommendation Format

Name this token by **ID** and **name**, state its exact fill_pct for the relevant tier, and explain the rotation rationale (high-traffic path, stalled migration, sole token for the store, etc.). If 429 errors were observed, explicitly recommend: (1) rotating more tokens to distribute load, or (2) adding delay between requests, or both — depending on whether multiple siblings exist.
