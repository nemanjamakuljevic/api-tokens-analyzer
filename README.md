# API Token Analyzer — Agentic Loop Demo

## Purpose

An internal Recharge tool for analyzing API-token usage by store, built as a hands-on
training vehicle for understanding **agentic loops** — the pattern the chatbot (and
tools like it) rely on. It shows how to give an LLM a system prompt, skill files, and
callable tools, then let *the model* drive: decide what to fetch, which framework to
apply, when to check its own work, and when it's done.

The design deliberately follows *Getting Real* — the smallest set of moving parts that
actually demonstrates the concept, not a framework.

---

## What makes this an agentic loop (not a pipeline)

The earlier version fetched all data up front, jammed every skill into the prompt, and
looped only to fill in scores. That is "fetch → process → complete", not a loop. This
version pushes the real decisions to the model:

| Property | How it shows up here |
|---|---|
| **Agent reasons about steps** | The model plans the run in extended thinking; nothing sequences the tools for it. |
| **Agent picks tools & timing** | `tool_choice: auto` — the model chooses among 5 tools each turn. |
| **Agent picks skills** | Skills are a *menu*. The model calls `load_skill` only for frameworks the data warrants — they are **not** all pre-loaded. |
| **Independent judge** | `verify_single_token_score` runs a **separate LLM call** with an adversarial prompt. It can reject; a rejected token cannot be emitted. |
| **Feedback changes direction** | A short window showing zero calls makes the agent **re-fetch a longer window** before it can justify cleanup. Judge objections force a re-score. |
| **Context accumulates** | Full message history (incl. thinking signatures) carries across turns. |
| **Iterations are unknowns** | The agent starts with *no usage data* and discovers state only by calling `fetch_token_usage`. |

---

## The loop

```
tokens (no usage)
      │
      ▼
 fetch_token_usage  ◄─── agent chooses the window; re-queries when inconclusive
      │
      ▼
 load_skill         ◄─── agent picks the framework(s) that fit each token
      │
      ▼
 score_single_token
      │
      ▼
 verify_single_token_score ──► INDEPENDENT JUDGE (separate LLM, adversarial)
      │                               │
      │◄──────── rejected ────────────┘   (re-score, re-verify)
      ▼ approved
 emit_recommendation   ◄─── gated: no emit without a passing verdict
      │
      ▼
 store-level summary, stop
```

### Decision tree (encoded in the skill files, chosen by the agent)

- Token idle over a **≥30-day** window → `token_cleanup`
- Token fill % over capacity / HTTP 429s → `security_audit`
- Active or rate-limited token, or stalled migration → `token_rotation`
- None of the above → `no_action`

The agent applies this tree itself — Python never routes it.

---

## Components

- **System prompt** — `backend/skills/system_prompt.md`: the agent's role, the leaky-bucket
  rate-limit model, and the loop it must follow.
- **Skill files** — `token_rotation.md`, `token_cleanup.md`, `security_audit.md`: scoring
  rubrics loaded on demand. Swap or add one without touching the agent.
- **Tools** — `fetch_token_usage`, `load_skill`, `score_single_token`,
  `verify_single_token_score`, `emit_recommendation` (in `backend/agent.py`).
- **Data layer** — `backend/splunk_client.py`: one path to Splunk for both the UI table
  and the agent's fetch tool, plus synthetic demo usage so the loop runs offline.
- **Thinking Process panel** — every fetch, skill load, score, judge verdict, and emit is
  streamed to the UI (SSE) as it happens, timestamped and auditable.
- **Output** — per-token recommendation cards (scores, reasoning, judge verdict) + a
  store-level summary.

**Stack**: React/Vite/TypeScript + Tailwind · FastAPI (Python 3.9) · Anthropic
(Claude Sonnet 5, extended thinking) · Snowflake + Splunk (live) or demo mode (offline).

---

## Inputs → expected outputs (demo store 20116)

Run store `20116` with no credentials and the agent fetches synthetic usage shaped to
exercise every path:

| Token | Signal | Expected action |
|---|---|---|
| 1152471 | fill 107% + 429s | `security_audit` |
| 1152461 | fill 58%, healthy | `token_rotation` |
| 1152453 / 1152394 | 0 calls (idle, old) | `token_cleanup` — **only after the agent re-queries a ≥30-day window** |
| 1151904 | near-idle but active | `no_action` |

Because the user's suggested window is 7 days, the agent must *notice* the window is too
short to justify cleanup and re-fetch 30 days on its own — the clearest visible sign the
loop is reasoning, not executing a script.

---

## Running

```bash
# backend
cd backend
pip install -r requirements.txt
# add ANTHROPIC_API_KEY to backend/.env  (Snowflake/Splunk creds optional — omit for demo)
uvicorn main:app --reload

# frontend
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Enter store `20116` (works offline in demo mode) and watch the Thinking Process panel.
Without Snowflake/Watchtower credentials the app serves demo data end-to-end; with them
it queries live `ODS.CURATED.API_TOKEN` and Splunk.
