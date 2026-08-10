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
| **Agent picks tools & timing** | `tool_choice: auto` — the model chooses among 14 tools each turn. Nothing is pre-fetched for it. |
| **Agent picks skills** | Skills are a *menu*. The model calls `load_skill` only for frameworks the data warrants — they are **not** all pre-loaded. |
| **Independent judge** | `verify_single_token_score` runs a **separate LLM call** with an adversarial prompt. It can reject; a rejected token cannot be emitted. It **fails closed** — an unreachable auditor is not an approving auditor. |
| **Bounded self-correction** | After 2 rejections for the same token the agent must fall back to `insufficient_data` or ask a human, so the re-score loop cannot spin. |
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
  rate-limit model, and its ground rules.
- **Decision tree** — `backend/skills/decision_tree.md`: the single source of routing
  truth — which tool to call, which skill to load. Appended to the system prompt.
  Tool descriptions say what a tool *does*; this says *when*. Thresholds live in the
  skill files. Nothing is duplicated across the three.
- **Skill files** — `token_rotation.md`, `token_cleanup.md`, `security_audit.md`,
  `rate_limit_pressure.md`: scoring rubrics loaded on demand.
- **Tools** — 14 schemas in `backend/tools.py`, executed in `backend/dispatch.py`.
- **Judge** — `backend/judge.py`: separate LLM call, adversarial prompt, fail-closed.
- **Loop** — `backend/agent.py`: one `while`, plus the guardrails around it.
- **Data layer** — `backend/splunk_client.py`: one path to Splunk for both the UI table
  and the agent's fetch tool. Live only — Snowflake and Watchtower credentials are
  required (there is no offline demo mode yet).
- **Thinking Process panel** — every fetch, skill load, score, judge verdict, and emit is
  streamed to the UI (SSE) as it happens, timestamped and auditable.
- **Output** — per-token recommendation cards (scores, reasoning, judge verdict) + a
  store-level summary.

**Stack**: React/Vite/TypeScript + Tailwind · FastAPI (Python 3.9) · Anthropic
(Claude Sonnet 5, extended thinking) · Snowflake + Splunk (live).

---

## Inputs → expected outputs (store 20116)

Store `20116` is the reference store — its live usage exercises every path:

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
# add ANTHROPIC_API_KEY to backend/.env
# Snowflake creds in .env + a Watchtower token at ~/.claude/watchtower-token are REQUIRED
uvicorn main:app --reload

# frontend
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Enter store `20116` and watch the Thinking Process panel. The app queries live
`ODS.CURATED.API_TOKEN` and Splunk — without those credentials every fetch tool
returns "authentication required" and the loop cannot get past intent.
