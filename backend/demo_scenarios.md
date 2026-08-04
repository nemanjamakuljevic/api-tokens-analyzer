# Demo Scenarios

Three scripted prompts for the free-form entry point (`POST /api/chat/ask`).
Use these to exercise and verify the three main paths.

---

## Scenario 1 — Vague (clarification required)

**Prompt:** `"something's wrong with my tokens"`

**Expected behavior:**
1. Turn 1 (forced): `record_intent` → `request_type: "general_question"`, `store_id: null`, `open_questions: ["no store_id given"]`
2. UI shows: indigo intent card with amber "Open: no store_id given"
3. Turn 2: `clarify_with_user` → user is asked which store they mean
4. After user reply: investigation proceeds with the store ID they provide

**Regression check:** No data-fetching tools should fire before `clarify_with_user`.

---

## Scenario 2 — Narrow / informational (skips roster lookup)

**Prompt:** `"why is token 1152471 in store 20116 getting rate limited?"`

**Expected behavior:**
1. Turn 1 (forced): `record_intent` → `request_type: "rate_limit_investigation"`, `store_id: 20116`, `requires_recommendation: false`
2. `lookup_store_tokens` is NOT called — the agent goes straight to `fetch_token_usage` / `fetch_429_errors`
3. Agent answers in prose citing fill percentages for token 1152471
4. `done` event: `token_scores: []`, prose `store_summary` only
5. UI shows: violet "Analysis Complete" card (no score cards)

**Regression check:** `lookup_store_tokens` should not appear in the step feed.

---

## Scenario 3 — Full audit (regression check for old behavior)

**Prompt:** `"audit store 20116"`

**Expected behavior:**
1. Turn 1 (forced): `record_intent` → `request_type: "full_audit"`, `store_id: 20116`, `requires_recommendation: true`
2. `lookup_store_tokens(store_id=20116)` → roster fetched from Snowflake
3. `fetch_token_usage` → usage data for all tokens
4. Score → verify → emit pipeline runs for each token
5. `done` event: `token_scores` non-empty, one card per token
6. UI shows: existing `TokenScoreCard` list

**Regression check:** Behavior should match the existing store-ID-first `/api/chat/start` flow.

---

## Running scenarios via curl

```bash
# Scenario 1
curl -N -X POST http://localhost:8000/api/chat/ask \
  -H "Content-Type: application/json" \
  -d '{"message": "something'\''s wrong with my tokens"}'

# Scenario 2
curl -N -X POST http://localhost:8000/api/chat/ask \
  -H "Content-Type: application/json" \
  -d '{"message": "why is token 1152471 in store 20116 getting rate limited?"}'

# Scenario 3
curl -N -X POST http://localhost:8000/api/chat/ask \
  -H "Content-Type: application/json" \
  -d '{"message": "audit store 20116"}'
```
