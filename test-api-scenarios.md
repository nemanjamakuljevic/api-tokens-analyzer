# Test API Scenarios

4 scenarija dizajnirana da trigguju različite tool putanje u agentic loop-u. Svaki scenario demonstrira drugačiji aspekt non-linearnog razmišljanja.

---

## Format podataka

Svaki scenario definiše:
- `tokens` — lista tokena u Snowflake formatu (`id`, `name`, `created_at`)
- `splunk_by_window` — Splunk `store_detail_usage` rows po window_days (`{1: [...], 30: [...]}`)

`store_detail_usage` format po redu: `access_token_id`, `method`, `full_path`, `status_code`, `count`

---

## Scenario A — Rate limit crisis

**Cilj:** Demonstrira da agent koristi `fetch_429_errors` i loada multiple skills (`rate_limit_pressure` + `security_audit`).

**Tokens:**
```json
[
  { "id": 1001, "name": "Checkout Integration", "created_at": "2024-01-15T00:00:00Z" },
  { "id": 1002, "name": "Admin Panel",           "created_at": "2024-06-01T00:00:00Z" }
]
```

**Splunk (1-day window):**
- Token 1001: ~180 calls, fill% ~90% na nonpro_1x1 (2 calls/s leak rate → 1001 treba da ima ~180 calls / 86400s ≈ 0.0021 calls/s... )

Napomena: Da bi fill% bio visok, treba store ID koji ima mali traffic limit. Primer za nonpro_1x1 (2/s) sa fill% = 90%:
`calls/s = 0.9 × 2 = 1.8/s → calls u 1 danu = 1.8 × 86400 = 155,520 calls`

Za realnu demo vrednost sa manjim brojevima, koristiti kraći window (npr. 1 sat = 3600s):
- Token 1001: 6,480 calls u 1h = 1.8/s → fill% ≈ 90% nonpro_1x1

Alternativa: koristiti window_days=1 ali sa pro_10x3 planom i fill% koji je iznad 50% čak i na tom nivou.

```python
# 1-day window, token 1001 agresivan
splunk_rows_1d = [
    {"access_token_id": 1001, "method": "GET",  "full_path": "/api/2021-11/subscriptions", "status_code": 200, "count": 120000},
    {"access_token_id": 1001, "method": "POST", "full_path": "/api/2021-11/charges",        "status_code": 429, "count": 45},
    {"access_token_id": 1001, "method": "GET",  "full_path": "/api/2021-11/customers",      "status_code": 200, "count": 8000},
    {"access_token_id": 1002, "method": "GET",  "full_path": "/api/2021-11/tokens",         "status_code": 200, "count": 150},
]
```

**Očekivana tool putanja:**
`fetch_token_usage(1d)` → vidi 45 rate_429 na 1001 → `fetch_429_errors(1d)` → `load_skill(rate_limit_pressure)` → `load_skill(security_audit)` → `score_single_token(1001)` → `verify` → `emit` → `score/verify/emit(1002)`

---

## Scenario B — Stalled migration

**Cilj:** Demonstrira prepoznavanje incomplete migration pattern-a (dva tokena istog imena, stariji nosi traffic).

**Tokens:**
```json
[
  { "id": 2001, "name": "Production API Key", "created_at": "2022-03-10T00:00:00Z" },
  { "id": 2002, "name": "Production API Key", "created_at": "2025-11-01T00:00:00Z" }
]
```

**Splunk (1-day window):**
```python
splunk_rows_1d = [
    {"access_token_id": 2001, "method": "POST", "full_path": "/api/2021-11/orders",         "status_code": 200, "count": 4200},
    {"access_token_id": 2001, "method": "GET",  "full_path": "/api/2021-11/subscriptions",  "status_code": 200, "count": 1800},
    {"access_token_id": 2002, "method": "GET",  "full_path": "/api/2021-11/subscriptions",  "status_code": 200, "count": 85},
]
```

Token 2001 (3 god. star) nosi 95%+ traffic-a, token 2002 (3 mes. star) minimal traffic → migration nije završena.

**Očekivana tool putanja:**
`fetch_token_usage(1d)` → vidi dva tokena istog imena, stariji dominantan → `load_skill(token_rotation)` → `score(2001)` rotation visok → `verify` → `emit(token_rotation)` za 2001

---

## Scenario C — Insufficient data + re-query

**Cilj:** Demonstrira non-linear tok — agent inicijalno bira kratak prozor, nema podataka, mora da proba duži prozor (ili emituje `insufficient_data`).

**Tokens:**
```json
[
  { "id": 3001, "name": "Legacy Reporting Key", "created_at": "2021-08-15T00:00:00Z" }
]
```

**Splunk:**
```python
# 1-day window → nema calls
splunk_rows_1d = []

# 30-day window → i dalje nema calls (token je zaista idle)
splunk_rows_30d = []
```

**Scenario flow:** Agent vidi 0 calls u 1d prozoru → nije siguran da li je token idle ili samo nije bio aktivan danas → re-query sa 30d → potvrđuje 0 calls → `load_skill(token_cleanup)` → score cleanup visoko (0 calls / 30d confirmed) → verify → `emit(token_cleanup)`

Alternativno: Agent emituje `insufficient_data` ako se drži 1d prozora bez re-query-a — to je validan ali manje edukativno interesantan ishod.

---

## Scenario D — Ambiguous / HITL

**Cilj:** Demonstrira `clarify_with_user` tool i pauziranje loop-a. Agent ne može sam da odredi šta treba da uradi.

**Tokens:**
```json
[
  { "id": 4001, "name": "Shopify Integration", "created_at": "2024-01-20T00:00:00Z" },
  { "id": 4002, "name": "Shopify Integration", "created_at": "2024-02-15T00:00:00Z" }
]
```

**Splunk (1-day window):**
```python
splunk_rows_1d = [
    {"access_token_id": 4001, "method": "POST", "full_path": "/api/2021-11/webhooks",       "status_code": 200, "count": 320},
    {"access_token_id": 4001, "method": "GET",  "full_path": "/api/2021-11/subscriptions",  "status_code": 200, "count": 180},
    {"access_token_id": 4002, "method": "POST", "full_path": "/api/2021-11/webhooks",       "status_code": 200, "count": 290},
    {"access_token_id": 4002, "method": "GET",  "full_path": "/api/2021-11/subscriptions",  "status_code": 200, "count": 160},
]
```

Oba tokena imaju slično korišćenje i isti naziv — razlika u created_at je samo mesec dana. Agent ne može pouzdano da odredi koja je "stara" i koja je "nova" verzija bez dodatnog konteksta.

**Očekivana tool putanja:**
`fetch_token_usage(1d)` → vidi dva tokena istog imena, oba aktivna, slični call rate-ovi → `clarify_with_user("Is a migration in progress between tokens 4001 and 4002?")` → čeka odgovor → nastavlja u zavisnosti od odgovora

**Moguća pitanja agenta:**
- "Is this migration still in progress, or are both tokens meant to be permanently active?"
- "Which of these two tokens is the older integration you plan to phase out?"

---

## Implementaciona napomena

Kada se implementira mock mod, `splunk_by_window` dict treba da podržava closest-match logiku:
```python
def get_mock_splunk(scenario: str, window_days: int) -> list:
    windows = SCENARIOS[scenario]["splunk_by_window"]
    # Uzeti closest window koji je >= traženom, ili najveći dostupni
    best = min((w for w in windows if w >= window_days), default=max(windows))
    return windows[best]
```

Ovo omogućava da agent bira bilo koji window i dobije smislen odgovor.
