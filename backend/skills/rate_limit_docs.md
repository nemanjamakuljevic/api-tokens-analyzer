# ReCharge API Rate Limits — Official Documentation
# Source: https://docs.getrecharge.com/docs/api-rate-limits

## Overview

Recharge implements rate limiting to maintain system stability and prevent abuse. The system uses a token-based approach allowing merchants to scale by distributing requests across multiple tokens.

## Core Mechanism

The platform employs a **leaky bucket algorithm** with these base specifications:

- **Bucket size:** 40 requests maximum
- **Leak rate:** 2 requests per second

This design permits occasional traffic spikes while supporting sustained operations. As stated in the docs: "If your app averages 2 calls per second, it will never trip a 429 error."

Note: stores may have a `rate_limit_multiplier` in `store.general_attributes` that scales the leak rate above the base 2/s. Always use the actual multiplier when available.

## Error Handling

Exceeding limits triggers:

> **Error 429 — Too Many Requests**

Merchants should implement retry logic with at least **2-second delays** when encountering this error.

## Mitigation Strategies

**Multiple Token Distribution:**
Cycling API calls across several tokens enables load balancing and reduces per-token request frequency. This is the primary use case for `token_rotation`.

**Application Optimization:**
- Eliminating redundant calls
- Using API extensions
- Leveraging async batch endpoints
- Implementing sleep intervals during non-time-sensitive processes

**Rate Limit Increases:**
Available through Recharge Support for all pricing tiers (Starter, Plus, Custom). All tokens must be replaced following any rate limit increase.

## Calculation Example

Making 40 requests followed by a 10-second pause allows **160 requests per minute** through strategic timing.

Sustained safe rate at base tier: **2 requests/second** (never exceeds bucket drain rate → no 429s).
