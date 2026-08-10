# Decision Tree

The single source of routing truth: which tool to call, and which skill to load.
Tool descriptions say what a tool *does*; this file says *when*. Individual skill
files own the scoring thresholds. If a threshold appears in two places, this file
is wrong — fix it here and delete the copy.

## 1. Establish scope

| Situation | Next step |
|---|---|
| store_id given, user wants a full audit | `lookup_store_tokens` |
| store_id given, user asked about specific token(s) | skip the roster — go straight to `fetch_token_usage` |
| token ID(s) given, no store_id | `lookup_token_store` — never ask the user for the store_id |
| neither store_id nor token IDs | `clarify_with_user` — there is nothing to look up |
| two same-named tokens, and migration status changes the urgency | `clarify_with_user` |

Output scope equals input scope. A roster fetched to *find* a token does not
widen the working set to the whole store.

## 2. Gather evidence

| Question being answered | Tool |
|---|---|
| Any scoring at all | `fetch_token_usage` — required before `score_single_token` |
| `rate_429 > 0`, or the user asks about rate limiting | `fetch_429_errors` |
| Why a token returns 401 / 403 / 422 / 5xx | `fetch_error_patterns`, then `load_recharge_status_codes` |
| "Which token made this call at 14:35?" | `fetch_token_activity` |
| "How does rate limiting work / how do we reduce 429s?" | `load_rate_limit_docs` |

Do not reason about error causes from endpoint names alone — fetch the breakdown.

**Inconclusive evidence is a reason to re-query, not to guess.** Zero calls over a
short window does not mean idle. Either re-fetch with a window of at least 30 days,
or emit `insufficient_data`.

## 3. Choose the skill(s)

Load only what the data warrants. Skills are a menu, not a checklist.

| Signal in the usage data | Load |
|---|---|
| `rate_429 > 0` for the token | `rate_limit_pressure` |
| Fill % at or over capacity, or a call rate far above sibling tokens | `security_audit` |
| Token is active, or two same-named tokens where the older carries most traffic | `token_rotation` |
| Zero calls over a window of **30 days or more** | `token_cleanup` |
| Zero calls over a shorter window | none yet — re-query 30 days, or `insufficient_data` |

Two skills can apply to one token (e.g. `rate_limit_pressure` + `security_audit`).
Load both and let the scores compete.

### Special case: stale rate-limit prefix

When **429 ratio > 20% of calls** AND **actual-tier fill % < 15%**, do not classify
this as a burst spike. Load `rate_limit_pressure` and follow its "Stale Rate-Limit
Prefix" section — this is a platform issue, not a token load issue.

## 4. Commit

`score_single_token` → `verify_single_token_score` → `emit_recommendation`.

- Nothing may be emitted without a passing verdict from the independent judge.
- Judge rejected → address the objections and re-score.
- Judge rejected the same token **twice** → stop re-scoring. Emit
  `insufficient_data` explaining what could not be verified, or `clarify_with_user`
  if a human decision would unblock it.
- Judge unreachable counts as a rejection. An unavailable auditor is not an
  approving auditor.

Informational requests (a question, a diagnosis, an explanation) need no scoring —
answer in prose. But if your answer implies a concrete action for a specific token,
route that token through score → verify → emit first.

## 5. Stop

When every token in scope is committed, write a short plain-prose summary and stop
calling tools. Not calling a tool is how you end the run.
