"""Fully agentic, nondeterministic token analysis loop.

The model — not Python — drives every decision:

  fetch_token_usage   the agent picks the observation window and pulls Splunk usage.
                      It has NO usage data until it calls this, and it may re-query a
                      longer window when what it finds is inconclusive.
  load_skill          the agent reads a token's usage, then decides which scoring
                      framework applies and loads only that one.
  score_single_token  records a 0–100 score per candidate action.
  verify_single_token_score
                      hands the score to an INDEPENDENT judge (a separate LLM call
                      with an adversarial prompt). The judge can reject; a rejected
                      token cannot be emitted until it is re-scored and re-approved.
  emit_recommendation finalizes — gated on a passing verdict.
  clarify_with_user   pauses the loop to ask the user a question mid-analysis.

Nothing about the run is fixed in advance: which tools fire, in what order, how many
windows get queried, which skills load, and whether a score survives audit all depend
on what the data turns out to be.
"""

import asyncio
import json
import os
import re
import subprocess
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

import anthropic

import splunk_client

SKILLS_DIR = Path(__file__).parent / "skills"
SKILL_NAMES = ["token_rotation", "token_cleanup", "security_audit", "rate_limit_pressure"]
MODEL = "claude-sonnet-5"
JUDGE_MODEL = "claude-sonnet-5"
FAST_MODEL = "claude-haiku-4-5-20251001"   # for structured scoring/verification turns

# Snowflake token TTL cache: token_id → (fetched_at_monotonic, record_dict)
_TOKEN_CACHE: dict[int, tuple[float, dict]] = {}
TOKEN_CACHE_TTL = 300  # seconds

# Snowflake store settings TTL cache: store_id → (fetched_at_monotonic, settings_dict)
_STORE_SETTINGS_CACHE: dict[int, tuple[float, dict]] = {}
STORE_SETTINGS_CACHE_TTL = 300  # seconds


def _fetch_store_settings_sync(conn, store_id: int) -> dict:
    """Fetch rate_limit_multiplier and internal_tokens_limit from store.general_attributes."""
    import json as _json
    import time as _time

    cached = _STORE_SETTINGS_CACHE.get(store_id)
    if cached and (_time.monotonic() - cached[0]) < STORE_SETTINGS_CACHE_TTL:
        return cached[1]

    cur = conn.cursor()
    cur.execute("SELECT general_attributes FROM store WHERE id = %s LIMIT 1", (store_id,))
    row = cur.fetchone()
    cur.close()

    if not row or not row[0]:
        result = {}
    else:
        raw = row[0]
        try:
            attrs = _json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        except Exception:
            attrs = {}
        result = {
            "rate_limit_multiplier": attrs.get("rate_limit_multiplier"),
            "internal_tokens_limit": attrs.get("internal_tokens_limit"),
        }

    _STORE_SETTINGS_CACHE[store_id] = (_time.monotonic(), result)
    return result

DEFAULT_WINDOW_DAYS = 1
CLEANUP_MIN_WINDOW_DAYS = 30

RATE_TIERS = {
    "nonpro_1x1": {"leak_rate": 2,  "bucket": 40},
    "nonpro_2x1": {"leak_rate": 4,  "bucket": 40},
    "pro_2x2":    {"leak_rate": 4,  "bucket": 80},
    "pro_5x3":    {"leak_rate": 10, "bucket": 120},
    "pro_10x3":   {"leak_rate": 20, "bucket": 120},
}

RECHARGE_STATUS_CODES = [
    {"code": 200, "name": "OK", "description": "Request processed successfully."},
    {"code": 201, "name": "Created", "description": "Resource created successfully."},
    {"code": 204, "name": "No Content", "description": "Request processed, no response body."},
    {"code": 400, "name": "Bad Request", "description": "Invalid request parameters or body. Check required fields and data types."},
    {"code": 401, "name": "Unauthorized", "description": "Invalid or missing API key in X-Recharge-Access-Token header."},
    {"code": 403, "name": "Forbidden", "description": "Valid API key but the token lacks permission for this resource or action."},
    {"code": 404, "name": "Not Found", "description": "The requested resource does not exist."},
    {"code": 422, "name": "Unprocessable Entity", "description": "Validation error — the request is structurally valid but semantically invalid (e.g. missing required relationship, invalid enum value)."},
    {"code": 429, "name": "Too Many Requests", "description": "Rate limit exceeded. ReCharge uses a token-bucket model; check Retry-After header for backoff. Sustained 429s indicate the token's avg calls/s exceeds the store's leak rate."},
    {"code": 500, "name": "Internal Server Error", "description": "Server-side error. Retry with exponential backoff; if persistent, it may indicate a ReCharge incident."},
    {"code": 503, "name": "Service Unavailable", "description": "Service temporarily unavailable (deploys, maintenance). Retry with backoff."},
]

MAX_TURNS = 60

# ── Tools ─────────────────────────────────────────────────────────────────────────

FETCH_TOKEN_USAGE_TOOL = {
    "name": "fetch_token_usage",
    "description": (
        "Pull Splunk API usage for this store over an observation window you choose. "
        "Returns per-token call counts, avg calls/s, rate-limit fill %, HTTP 429 counts, "
        "and endpoint breakdown. Choose the window based on what you are investigating."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "description": "Observation window in days.",
            },
            "reason": {
                "type": "string",
                "description": "Brief note about what you expect to find or why you chose this window.",
            },
        },
        "required": ["window_days"],
    },
}

LOAD_SKILL_TOOL = {
    "name": "load_skill",
    "description": (
        "Load ONE scoring framework's criteria, chosen from what the usage data shows. "
        "token_rotation: active tokens or stalled migrations (older token still carries most traffic). "
        "token_cleanup: tokens idle over a >=30-day window. "
        "security_audit: fill % over capacity or anomalous call-rate spikes. "
        "rate_limit_pressure: load when rate_429 > 0 for a token. Evaluates BOTH fill % (sustained load) "
        "AND the 429 ratio (429_count ÷ total_calls). A high 429 ratio (>10% of calls) warrants action "
        "even at low fill %, because frequent burst hits are a structural integration problem. "
        "Load the skill(s) relevant to a token before scoring it — do not assume all apply."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "enum": SKILL_NAMES},
        },
        "required": ["skill_name"],
    },
}

SCORE_SINGLE_TOKEN_TOOL = {
    "name": "score_single_token",
    "description": (
        "Record your scoring analysis for one token across rotation, cleanup, and security audit. "
        "Score only against criteria from skills you have loaded. Call once per token."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "token_id":                   {"type": "integer"},
            "token_name":                 {"type": "string"},
            "rotation_score":             {"type": "integer", "minimum": 0, "maximum": 100},
            "rotation_reasoning":         {"type": "string"},
            "cleanup_score":              {"type": "integer", "minimum": 0, "maximum": 100},
            "cleanup_reasoning":          {"type": "string"},
            "security_audit_score":       {"type": "integer", "minimum": 0, "maximum": 100},
            "security_audit_reasoning":   {"type": "string"},
        },
        "required": [
            "token_id", "token_name",
            "rotation_score", "rotation_reasoning",
            "cleanup_score", "cleanup_reasoning",
            "security_audit_score", "security_audit_reasoning",
        ],
    },
}

VERIFY_SINGLE_TOKEN_TOOL = {
    "name": "verify_single_token_score",
    "description": (
        "Submit a scored token to an INDEPENDENT auditor for review. The auditor "
        "re-examines the data against the skill criteria and either approves or rejects "
        "with objections. You must pass this before you can emit a recommendation. "
        "If rejected, re-score to address the objections, then verify again."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "token_id": {"type": "integer"},
        },
        "required": ["token_id"],
    },
}

EMIT_RECOMMENDATION_TOOL = {
    "name": "emit_recommendation",
    "description": (
        "Finalize and commit your recommendation for a token — only after it has passed "
        "verification. Cite fill percentages in recommendation text, not raw calls/s. "
        "Use 'insufficient_data' when the observation window is too short to draw a "
        "definitive conclusion — include a note to retry with a longer window."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "token_id":           {"type": "integer"},
            "token_name":         {"type": "string"},
            "recommended_action": {
                "type": "string",
                "enum": ["token_rotation", "token_cleanup", "security_audit",
                         "no_action", "insufficient_data"],
            },
            "recommendation": {"type": "string"},
        },
        "required": ["token_id", "token_name", "recommended_action", "recommendation"],
    },
}

CLARIFY_WITH_USER_TOOL = {
    "name": "clarify_with_user",
    "description": (
        "Pause the analysis and ask the user a clarifying question. Use ONLY when the "
        "answer would change which tool gets called next or which recommendation gets made. "
        "Hard blockers that always require clarification: no store_id AND no token IDs in "
        "the request (truly nothing to look up); two same-named tokens where migration "
        "status changes urgency. "
        "Do NOT ask about: store_id when token IDs are known — use lookup_token_store "
        "instead; vague timeframes (default to 7 days and disclose); the meaning of "
        "'unused' (token_cleanup.md already defines this); anything answerable from data alone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Question to display to the user."},
            "context":  {"type": "string", "description": "Why you need this information."},
        },
        "required": ["question"],
    },
}

RECORD_INTENT_TOOL = {
    "name": "record_intent",
    "description": (
        "Record your understanding of the user's request before investigating. "
        "Call this FIRST, before any data-fetching tool, so the analysis has a clear target. "
        "If store_id is absent and no token IDs were mentioned, add 'no store_id given' to "
        "open_questions — the very next step should be clarify_with_user."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request_type": {
                "type": "string",
                "enum": [
                    "full_audit", "single_token_diagnosis", "rate_limit_investigation",
                    "cleanup_request", "security_concern", "general_question",
                ],
            },
            "store_id": {
                "type": ["integer", "null"],
                "description": "Extracted from the request, if present.",
            },
            "token_ids_mentioned": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Any specific token IDs the user named.",
            },
            "timeframe_hint": {
                "type": "string",
                "description": "Any timeframe language in the request, e.g. 'yesterday'. Empty if none.",
            },
            "requires_recommendation": {
                "type": "boolean",
                "description": (
                    "True if the user wants an actionable recommendation (rotate/cleanup/audit). "
                    "False if this is an informational question or diagnosis."
                ),
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything genuinely blocking — e.g. no store_id given, ambiguous scope.",
            },
        },
        "required": ["request_type", "requires_recommendation"],
    },
}

LOOKUP_STORE_TOKENS_TOOL = {
    "name": "lookup_store_tokens",
    "description": (
        "Fetch the token roster (id, name, created_at) for a store from Snowflake. "
        "Use ONLY for full audits or when you need to discover what tokens exist. "
        "NEVER call this if the user already named specific token IDs — go straight to "
        "fetch_token_usage instead. Calling this unnecessarily fetches all tokens when "
        "only one was asked about."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "store_id": {"type": "integer"},
        },
        "required": ["store_id"],
    },
}

LOOKUP_TOKEN_STORE_TOOL = {
    "name": "lookup_token_store",
    "description": (
        "Given a token ID, look up which store it belongs to in Snowflake. "
        "Use this whenever the user names a specific token ID but does not provide a store_id — "
        "do NOT ask the user for the store_id when you can resolve it from Snowflake. "
        "Returns the store_id, token name, and created_at for the token."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "token_id": {"type": "integer", "description": "The API token ID to look up."},
        },
        "required": ["token_id"],
    },
}

FETCH_429_ERRORS_TOOL = {
    "name": "fetch_429_errors",
    "description": (
        "Deep-dive on rate-limiting: query Splunk specifically for HTTP 429 responses "
        "for this store. Use after fetch_token_usage reveals elevated fill percentages "
        "or 429 counts, or when the user asks about rate-limiting issues. Returns "
        "per-token 429 counts, top rate-limited endpoints, and time distribution. "
        "Complements fetch_token_usage by focusing exclusively on rate-limit signal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "description": "Window to search for 429 errors. Match or exceed the window from fetch_token_usage.",
            },
            "token_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Specific token IDs to analyze. Empty list = all store tokens.",
            },
        },
        "required": ["window_days"],
    },
}

LOAD_RATE_LIMIT_DOCS_TOOL = {
    "name": "load_rate_limit_docs",
    "description": (
        "Load the official ReCharge API rate-limit documentation (leaky bucket model, "
        "bucket size, leak rate, 429 handling, mitigation strategies, rate limit increases). "
        "Use when answering questions about how rate limiting works, why 429s occur, "
        "retry strategies, or how to reduce rate-limit pressure."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

LOAD_RECHARGE_STATUS_CODES_TOOL = {
    "name": "load_recharge_status_codes",
    "description": (
        "Load the ReCharge API 2021-11 HTTP status code reference. Use when interpreting "
        "error patterns in Splunk data (4xx/5xx counts), explaining what specific codes "
        "mean in the ReCharge context, or reasoning about 429 rate-limit behavior and "
        "retry semantics. Also triggers a status codes modal in the UI for the user."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TOOLS = [
    RECORD_INTENT_TOOL,
    LOOKUP_TOKEN_STORE_TOOL,
    LOOKUP_STORE_TOKENS_TOOL,
    FETCH_TOKEN_USAGE_TOOL,
    FETCH_429_ERRORS_TOOL,
    LOAD_SKILL_TOOL,
    LOAD_RECHARGE_STATUS_CODES_TOOL,
    LOAD_RATE_LIMIT_DOCS_TOOL,
    SCORE_SINGLE_TOKEN_TOOL,
    VERIFY_SINGLE_TOKEN_TOOL,
    EMIT_RECOMMENDATION_TOOL,
    CLARIFY_WITH_USER_TOOL,
]

JUDGE_VERDICT_TOOL = {
    "name": "verdict",
    "description": "Record your audit verdict for the token's scores.",
    "input_schema": {
        "type": "object",
        "properties": {
            "approved":   {"type": "boolean"},
            "objections": {"type": "array", "items": {"type": "string"}},
            "reasoning":  {"type": "string"},
        },
        "required": ["approved", "reasoning"],
    },
}


# ── Utilities ──────────────────────────────────────────────────────────────────

def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Claude CLI mode helpers ────────────────────────────────────────────────────────

_TOOL_USE_MARKER = "TOOL_CALL_JSON:"


async def _call_claude_cli(prompt: str, model: str = None) -> str:
    """Call `claude -p` subprocess, stripping API key env vars so session auth is used."""
    use_model = model or MODEL
    cmd = ["claude", "-p", "--output-format", "json", "--allowed-tools", "", "--model", use_model]
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, input=prompt, capture_output=True, text=True, timeout=180, env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude CLI call timed out after 180s")
    except FileNotFoundError:
        raise RuntimeError("'claude' CLI not found — install Claude Code and run `claude login`.")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"claude CLI exited with code {proc.returncode}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unexpected CLI output: {proc.stdout[:200]}")
    if envelope.get("is_error"):
        raise RuntimeError(envelope.get("result", "CLI error"))
    return envelope.get("result", "")


def _build_cli_prompt(system: str, messages: list, tools: list, force_tool: str = None) -> str:
    """Build a plain-text prompt for CLI mode with embedded tool definitions."""
    lines = [system.strip(), ""]

    if tools:
        tool_json = json.dumps(tools, indent=2)
        lines += [
            "=" * 70,
            "## AVAILABLE TOOLS",
            "",
            tool_json,
            "",
            "## TOOL CALL PROTOCOL",
            "",
            "To call a tool, end your response with exactly this on a new line:",
            f"  {_TOOL_USE_MARKER} {{\"name\": \"tool_name\", \"input\": {{...}}}}",
            "",
            "Rules: only ONE tool call per response; no text after the JSON; "
            "if done (no tool needed), respond normally without the marker.",
            "=" * 70,
            "",
        ]

    if force_tool:
        lines += [
            f"IMPORTANT: You MUST call the `{force_tool}` tool in this response.",
            "",
        ]

    lines.append("## CONVERSATION")
    lines.append("")

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"### {role.upper()}")
            lines.append(content)
            lines.append("")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    lines.append(f"### {role.upper()}")
                    lines.append(block.get("text", ""))
                    lines.append("")
                elif btype == "thinking":
                    pass
                elif btype == "tool_use":
                    lines.append(f"### ASSISTANT (tool call: {block.get('name')})")
                    lines.append(f"Input: {json.dumps(block.get('input', {}))}")
                    lines.append("")
                elif btype == "tool_result":
                    lines.append("### TOOL RESULT")
                    lines.append(str(block.get("content", "")))
                    lines.append("")

    lines.append("### ASSISTANT")
    return "\n".join(lines)


def _parse_cli_response(response: str, known_tools: set) -> tuple:
    """Parse CLI response into (text_or_None, tool_call_or_None)."""
    marker_pos = response.rfind(_TOOL_USE_MARKER)
    if marker_pos == -1:
        return response.strip(), None

    pre_text = response[:marker_pos].strip()
    call_json_text = response[marker_pos + len(_TOOL_USE_MARKER):].strip()

    try:
        call_data = json.loads(call_json_text)
        name = call_data.get("name", "")
        inp = call_data.get("input", {})
        if name in known_tools:
            tool_call = {
                "type": "tool_use",
                "id": f"cli_{uuid.uuid4().hex[:12]}",
                "name": name,
                "input": inp,
            }
            return pre_text or None, tool_call
    except (json.JSONDecodeError, AttributeError):
        pass

    return response.strip(), None


async def _run_judge_cli(token_record: dict, score_data: dict, window_days: float) -> dict:
    """Run the independent judge using the CLI subprocess."""
    scores_map = {
        "token_rotation": score_data.get("rotation_score", 0),
        "token_cleanup":  score_data.get("cleanup_score", 0),
        "security_audit": score_data.get("security_audit_score", 0),
    }
    best = max(scores_map, key=lambda k: scores_map[k])
    best = best if max(scores_map.values()) >= 20 else "no_action"

    fp = token_record.get("fill_pct") or {}
    fill_str = " | ".join(f"{tier}: {fp.get(tier, 0)}%" for tier in RATE_TIERS) if fp else "n/a"
    data_block = (
        f"Token id={token_record['id']} name=\"{token_record['name']}\" age={token_record.get('age_days', -1)}d\n"
        f"calls={token_record.get('splunk_count')} calls/s={token_record.get('calls_per_second')} "
        f"rate_429={token_record.get('rate_429', 0)}\n"
        f"fill%: {fill_str}\n"
        f"shares_name_with={token_record.get('other_tokens_with_same_name', 0)} other token(s)\n"
        f"Observation window: {window_days} days (cleanup requires >= {CLEANUP_MIN_WINDOW_DAYS})\n"
    )
    scores_block = (
        f"rotation={scores_map['token_rotation']} — {score_data.get('rotation_reasoning', '')}\n"
        f"cleanup={scores_map['token_cleanup']} — {score_data.get('cleanup_reasoning', '')}\n"
        f"security_audit={scores_map['security_audit']} — {score_data.get('security_audit_reasoning', '')}\n"
        f"Implied action: {best}"
    )
    skills_block = "\n\n".join(
        f"--- {n} ---\n{_load_text(SKILLS_DIR / f'{n}.md')}" for n in SKILL_NAMES
    )
    judge_prompt = (
        f"You are a SKEPTICAL independent auditor of API-token scoring.\n"
        f"Approve ONLY if every score is fully supported by the data.\n"
        f"Reject if: cleanup scored on active token (calls/s > 0); cleanup window < {CLEANUP_MIN_WINDOW_DAYS} days; "
        f"score unsupported by data; recommended action contradicts usage.\n"
        f"ALWAYS approve 'insufficient_data' when 0 calls and window < {CLEANUP_MIN_WINDOW_DAYS} days.\n\n"
        f"TOKEN DATA:\n{data_block}\n\nPROPOSED SCORES:\n{scores_block}\n\nSKILL CRITERIA:\n{skills_block}\n\n"
        f"Respond with EXACTLY this JSON (no other text):\n"
        f'{{\"approved\": true, \"objections\": [], \"reasoning\": \"...\"}}'
    )
    try:
        response = await _call_claude_cli(judge_prompt, JUDGE_MODEL)
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            v = json.loads(response[start:end])
            return {
                "approved": bool(v.get("approved", False)),
                "objections": v.get("objections", []),
                "reasoning": v.get("reasoning", ""),
            }
    except Exception:
        pass
    return {"approved": True, "objections": [], "reasoning": "Judge unavailable (CLI mode) — auto-approved."}


async def _stream_turn_cli(
    messages: list,
    system_prompt: str,
    tools: list,
    ctx: dict,
    tool_choice: dict = None,
    model: str = None,
) -> AsyncGenerator[str, None]:
    """One agent turn using `claude -p` CLI subprocess (no streaming — response appears at once)."""
    ctx["_turn_assistant_content"] = []
    ctx["_turn_tool_results"] = []
    ctx["_turn_has_tool_use"] = False
    ctx["_clarification_pending"] = None
    ctx["_turn_error"] = None

    known_tools = {t["name"] for t in tools}
    force_tool = tool_choice.get("name") if tool_choice and tool_choice.get("type") == "tool" else None

    prompt = _build_cli_prompt(system_prompt, messages, tools, force_tool=force_tool)

    try:
        response = await _call_claude_cli(prompt, model)
    except RuntimeError as e:
        yield _sse({"type": "error", "message": str(e)})
        ctx["_turn_error"] = str(e)
        return

    text, tool_call = _parse_cli_response(response, known_tools)

    if tool_call:
        ctx["_turn_has_tool_use"] = True
        name = tool_call["name"]
        inp = tool_call["input"]
        ctx["_turn_assistant_content"].append(tool_call)

        if name == "clarify_with_user":
            ctx["_clarification_pending"] = {
                "tool_use_id": tool_call["id"],
                "question": inp.get("question", ""),
                "context": inp.get("context", ""),
            }
            yield _sse({
                "type": "clarification_request",
                "question": inp.get("question", ""),
                "context": inp.get("context", ""),
                "ts": _ts(),
            })
        else:
            result_text, sse_event = await _dispatch_tool(name, inp, ctx)
            if text:
                sse_event["narration"] = text[:500]
            yield _sse({**sse_event, "ts": _ts()})
            for extra_event in ctx.pop("_pending_sse_events", []):
                yield _sse({**extra_event, "ts": _ts()})
            ctx["_turn_tool_results"].append({
                "type": "tool_result",
                "tool_use_id": tool_call["id"],
                "content": result_text,
            })
    else:
        if text:
            ctx["_turn_assistant_content"].append({"type": "text", "text": text})
            yield _sse({"type": "content_chunk", "delta": text, "ts": _ts()})


def _clean_str(text: str) -> str:
    text = re.sub(r"</\w[\w\-]*>\s*$", "", text.strip())
    text = re.sub(r"<parameter\b[^>]*>.*$", "", text.strip(), flags=re.DOTALL)
    return text.strip()


def _build_detail_by_token(detail_raw: list) -> dict:
    result = {}
    for row in detail_raw:
        tid = str(row.get("access_token_id", ""))
        if not tid:
            continue
        result.setdefault(tid, []).append(row)
    return result


def _build_usage_lookup(raw: list) -> dict:
    lookup = {}
    for row in raw:
        tid = str(row.get("access_token_id", ""))
        if not tid:
            continue
        try:
            # count=1 when row has no 'count' key (raw request row = 1 request)
            cnt = int(row.get("count", 1))
        except (ValueError, TypeError):
            cnt = 1
        lookup[tid] = lookup.get(tid, 0) + cnt
    return lookup


def _build_endpoint_summary(detail_rows: list) -> str:
    endpoint_counts: dict = {}
    for row in detail_rows:
        method = row.get("method", "")
        path = row.get("full_path", "")
        status = str(row.get("status_code", ""))
        try:
            cnt = int(row.get("count", 1))  # raw row = 1 request
        except (ValueError, TypeError):
            cnt = 1
        key = f"{method} {path}"
        if key not in endpoint_counts:
            endpoint_counts[key] = {"total": 0, "by_status": {}}
        endpoint_counts[key]["total"] += cnt
        endpoint_counts[key]["by_status"][status] = endpoint_counts[key]["by_status"].get(status, 0) + cnt

    top = sorted(endpoint_counts.items(), key=lambda x: -x[1]["total"])[:5]
    parts = []
    for endpoint, edata in top:
        total = edata["total"]
        by_status = edata["by_status"]
        count_429 = by_status.get("429", 0)
        count_5xx = sum(v for k, v in by_status.items() if k.startswith("5"))
        note = ""
        if count_429:
            note += f", {count_429}×429"
        if count_5xx and count_5xx != count_429:
            note += f", {count_5xx - count_429}×5xx"
        parts.append(f"{endpoint}: {total}{note}")
    return " | ".join(parts)


def _enrich(store_id: int, tokens: list, detail_raw: list, window_seconds: int) -> dict:
    store_total = _build_usage_lookup(detail_raw)
    rate_limit_429: dict = {}
    for row in detail_raw:
        if str(row.get("status_code", "")) == "429":
            tid = str(row.get("access_token_id", ""))
            try:
                cnt = int(row.get("count", 1))  # raw row = 1 request
            except (ValueError, TypeError):
                cnt = 1
            rate_limit_429[tid] = rate_limit_429.get(tid, 0) + cnt

    detail_by_token = _build_detail_by_token(detail_raw)
    known_ids = {str(t["id"]) for t in tokens}

    orphaned = [
        {"id": tid, "count": cnt, "calls_per_second": round(cnt / window_seconds, 4)}
        for tid, cnt in store_total.items()
        if tid not in known_ids and cnt > 0
    ]

    now = datetime.now(timezone.utc)
    enriched = []
    for t in tokens:
        try:
            dt = datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - dt).days)
        except Exception:
            age_days = -1

        tid_str = str(t["id"])
        splunk_count = store_total.get(tid_str, 0)
        calls_per_second = round(splunk_count / window_seconds, 4)
        fill_pct = {
            tier: round(calls_per_second / info["leak_rate"] * 100, 1)
            for tier, info in RATE_TIERS.items()
        }

        enriched.append({
            "id": t["id"],
            "name": t["name"],
            "created_at": t["created_at"],
            "age_days": age_days,
            "splunk_count": splunk_count,
            "calls_per_second": calls_per_second,
            "fill_pct": fill_pct,
            "rate_429": rate_limit_429.get(tid_str, 0),
            "detail": detail_by_token.get(tid_str, []),
        })

    name_counts = Counter(t["name"] for t in enriched)
    for t in enriched:
        t["other_tokens_with_same_name"] = name_counts[t["name"]] - 1

    return {
        "store_id": store_id,
        "tokens": enriched,
        "total": len(enriched),
        "orphaned_tokens": orphaned,
        "window_seconds": window_seconds,
    }


def _format_store_settings(settings: dict) -> str:
    if not settings:
        return ""
    parts = []
    rlm = settings.get("rate_limit_multiplier")
    itl = settings.get("internal_tokens_limit")
    if rlm is not None:
        parts.append(f"rate_limit_multiplier={rlm}")
    if itl is not None:
        parts.append(f"internal_tokens_limit={itl}")
    if not parts:
        return ""
    return f"Store settings: {', '.join(parts)}.\n"


def _usage_summary(enriched: dict, store_settings: dict = None) -> str:
    tokens = enriched["tokens"]
    window = enriched["window_seconds"]
    orphaned = enriched.get("orphaned_tokens", [])
    window_days = round(window / 86400, 2)

    known_calls = sum(t["splunk_count"] for t in tokens if t["splunk_count"] is not None)
    orphaned_calls = sum(o["count"] for o in orphaned)
    total_calls = known_calls + orphaned_calls
    total_cps = round(total_calls / window, 4)

    store_fill = " | ".join(
        f"{tier}: {round(total_cps / info['leak_rate'] * 100, 1)}%"
        for tier, info in RATE_TIERS.items()
    ) if total_calls else "no usage data"

    # Surface actual rate limit from store settings when available
    settings = store_settings or {}
    rate_mult = settings.get("rate_limit_multiplier")
    actual_tier_line = ""
    if rate_mult:
        try:
            actual_leak_rate = float(rate_mult) * 2  # base rate is 2 calls/s
            actual_fill = round(total_cps / actual_leak_rate * 100, 1) if total_calls else 0
            actual_tier_line = (
                f"ACTUAL store rate limit (rate_limit_multiplier={rate_mult} → "
                f"leak_rate={actual_leak_rate}/s): store fill = {actual_fill}%"
            )
        except (TypeError, ValueError):
            pass

    lines = [
        f"Usage window: {window_days} days ({window}s)",
        f"Store total: {total_calls} calls (known={known_calls}, orphaned={orphaned_calls}), avg {total_cps} calls/s",
    ]
    if actual_tier_line:
        lines.append(actual_tier_line)
    lines += [
        f"Store fill % by tier (for reference): {store_fill}",
        "",
        "Fill % = avg_calls_per_second ÷ leak_rate × 100. Tiers:",
        "  nonpro_1x1: 2/s·40 | nonpro_2x1: 4/s·40 | pro_2x2: 4/s·80 | pro_5x3: 10/s·120 | pro_10x3: 20/s·120",
        "",
        "Per-token usage:",
    ]
    for t in tokens:
        parts = [f"  id={t['id']}", f"name=\"{t['name']}\"", f"age={t['age_days']}d",
                 f"calls={t['splunk_count']}", f"calls/s={t['calls_per_second']}"]
        fp = t["fill_pct"]
        if fp:
            parts.append("fill%=[" + " | ".join(f"{tier}: {fp[tier]}%" for tier in RATE_TIERS) + "]")
        if t["rate_429"]:
            parts.append(f"rate_limited_429={t['rate_429']}")
        if t["other_tokens_with_same_name"]:
            parts.append(f"shares_name_with={t['other_tokens_with_same_name']} other(s)")
        lines.append(", ".join(parts))
        if t.get("detail"):
            ep = _build_endpoint_summary(t["detail"])
            if ep:
                lines.append(f"    Endpoints: {ep}")

    if orphaned:
        lines.append("")
        lines.append("ORPHANED (active in Splunk but not in Snowflake):")
        for o in orphaned:
            lines.append(f"  id={o['id']}, calls={o['count']}, calls/s={o['calls_per_second']}")
    return "\n".join(lines)


# ── Independent judge ─────────────────────────────────────────────────────────────

async def _run_judge(client, token_record: dict, score_data: dict, window_days: float) -> dict:
    scores_map = {
        "token_rotation": score_data.get("rotation_score", 0),
        "token_cleanup":  score_data.get("cleanup_score", 0),
        "security_audit": score_data.get("security_audit_score", 0),
    }
    best = max(scores_map, key=lambda k: scores_map[k])
    best = best if max(scores_map.values()) >= 20 else "no_action"

    fp = token_record.get("fill_pct") or {}
    fill_str = " | ".join(f"{tier}: {fp.get(tier, 0)}%" for tier in RATE_TIERS) if fp else "n/a"
    data_block = (
        f"Token id={token_record['id']} name=\"{token_record['name']}\" age={token_record['age_days']}d\n"
        f"calls={token_record['splunk_count']} calls/s={token_record['calls_per_second']} "
        f"rate_429={token_record['rate_429']}\n"
        f"fill%: {fill_str}\n"
        f"shares_name_with={token_record['other_tokens_with_same_name']} other token(s)\n"
        f"Observation window: {window_days} days (cleanup requires ≥ {CLEANUP_MIN_WINDOW_DAYS})\n"
    )
    scores_block = (
        f"rotation={scores_map['token_rotation']} — {score_data.get('rotation_reasoning', '')}\n"
        f"cleanup={scores_map['token_cleanup']} — {score_data.get('cleanup_reasoning', '')}\n"
        f"security_audit={scores_map['security_audit']} — {score_data.get('security_audit_reasoning', '')}\n"
        f"Implied recommended action (highest score): {best}"
    )
    skills_block = "\n\n".join(
        f"--- {name} ---\n{_load_text(SKILLS_DIR / f'{name}.md')}" for name in SKILL_NAMES
    )

    judge_system = (
        "You are a SKEPTICAL, independent auditor of API-token scoring. You did not "
        "produce these scores. Your job is to find contradictions, not to be agreeable. "
        "Approve ONLY if every score is fully supported by the data AND the recommended action "
        "is correct. Reject with specific objections if: cleanup is scored on an active token "
        f"(calls/s > 0), cleanup is scored on a window < {CLEANUP_MIN_WINDOW_DAYS} days, "
        "a score has no supporting signal in the data, or the recommended action contradicts "
        "the usage pattern. "
        "ALWAYS approve 'insufficient_data' when the token has 0 calls and the window is "
        f"< {CLEANUP_MIN_WINDOW_DAYS} days — this is the correct honest answer when evidence is thin. "
        "Default to rejecting when uncertain about definitive actions; never reject insufficient_data."
    )
    judge_user = (
        f"TOKEN DATA:\n{data_block}\n\nPROPOSED SCORES:\n{scores_block}\n\n"
        f"SKILL CRITERIA:\n{skills_block}\n\nAudit these scores and record your verdict."
    )

    try:
        resp = await client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=1200,
            system=judge_system,
            tools=[JUDGE_VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "verdict"},
            messages=[{"role": "user", "content": judge_user}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "verdict":
                v = block.input
                return {
                    "approved": bool(v.get("approved", False)),
                    "objections": v.get("objections", []),
                    "reasoning": v.get("reasoning", ""),
                }
    except anthropic.APIError:
        pass
    return {"approved": True, "objections": [], "reasoning": "Judge unavailable — auto-approved (fail-open)."}


# ── Tool dispatch ─────────────────────────────────────────────────────────────────

async def _dispatch_tool(name: str, inp: dict, ctx: dict):
    """Execute one tool call. Returns (result_text, sse_event)."""

    if name == "fetch_token_usage":
        window_days = int(inp.get("window_days", DEFAULT_WINDOW_DAYS))
        window_secs = window_days * 86400
        ctx["fetch_count"] = ctx.get("fetch_count", 0) + 1
        is_re_query = ctx["fetch_count"] > 1
        prev_window_days = ctx["max_window_days"] if is_re_query else None
        # Filter to known token IDs so the query is scoped; fall back to store-wide.
        known_token_ids = [t["id"] for t in ctx.get("tokens", []) if t.get("id")]
        try:
            res = await asyncio.to_thread(
                splunk_client.fetch_store_usage, ctx["store_id"], window_days, "days",
                token_ids=known_token_ids or None,
            )
            if res.get("redirect"):
                return (
                    "Splunk authentication required — cannot fetch live usage. "
                    "Authenticate via the Splunk tab and retry.",
                    {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required",
                     "detail": res.get("splunk_url", "")},
                )
            usage = {"store_detail_usage": res["store_detail_usage"]}
        except (ValueError, Exception) as splunk_err:
            return (
                f"Splunk fetch failed: {splunk_err}. Cannot proceed without usage data.",
                {"type": "step", "tool": name, "label": "Fetch error", "detail": str(splunk_err)},
            )

        enriched = _enrich(ctx["store_id"], ctx["tokens"], usage["store_detail_usage"], window_secs)
        ctx["window_seconds"] = window_secs
        ctx["max_window_days"] = max(ctx["max_window_days"], window_days)
        for rec in enriched["tokens"]:
            ctx["token_records"][rec["id"]] = rec
        ctx["orphaned"] = enriched.get("orphaned_tokens", [])
        # In free-form mode without a roster, treat Splunk-only tokens as real records
        # so score/verify/emit tools can find them by token_id.
        if not ctx["tokens"]:
            for orec in ctx["orphaned"]:
                tid = orec["id"]
                if tid not in ctx["token_records"]:
                    ctx["token_records"][int(tid)] = {
                        "id": int(tid),
                        "name": f"id={tid}",
                        "splunk_count": orec.get("count", 0),
                        "calls_per_second": orec.get("calls_per_second", 0),
                        "fill_pct": {
                            tier: round(orec.get("calls_per_second", 0) / info["leak_rate"] * 100, 1)
                            for tier, info in RATE_TIERS.items()
                        },
                        "rate_429": 0,
                        "detail": [],
                    }
        summary = _usage_summary(enriched, ctx.get("store_settings"))
        splunk_rows = [
            {
                "id": t["id"],
                "name": t["name"],
                "calls": t["splunk_count"],
                "cps": t["calls_per_second"],
                "rate_429": t["rate_429"],
                "fill_top": round(max(t["fill_pct"].values()), 1) if t.get("fill_pct") else 0,
            }
            for t in enriched["tokens"]
        ]
        return (
            f"Fetched usage over a {window_days}-day window.\n\n{summary}",
            {"type": "step", "tool": name,
             "call": f"fetch_token_usage(window_days={window_days})",
             "label": f"Fetched usage: {window_days}-day window",
             "detail": inp.get("reason", f"{enriched['total']} tokens, {len(ctx['orphaned'])} orphaned"),
             "splunk_rows": splunk_rows,
             "re_query": is_re_query,
             "prev_window_days": prev_window_days},
        )

    if name == "fetch_429_errors":
        window_days = int(inp.get("window_days", DEFAULT_WINDOW_DAYS))
        window_secs = window_days * 86400
        token_ids_filter = {str(i) for i in inp.get("token_ids", [])}

        # Pass token_ids from tool input (or fall back to known roster) to scope the query.
        splunk_token_ids = list(inp.get("token_ids", [])) or [t["id"] for t in ctx.get("tokens", []) if t.get("id")]
        try:
            res = await asyncio.to_thread(
                splunk_client.fetch_store_usage, ctx["store_id"], window_days, "days",
                token_ids=splunk_token_ids or None,
            )
            if res.get("redirect"):
                return (
                    "Splunk authentication required.",
                    {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required", "detail": ""},
                )
            raw = res["store_detail_usage"]
        except Exception as splunk_err:
            return (
                f"Splunk fetch failed: {splunk_err}. Cannot retrieve 429 data.",
                {"type": "step", "tool": name, "label": "Fetch error", "detail": str(splunk_err)},
            )

        rows_429 = [r for r in raw if str(r.get("status_code", "")) == "429"]
        if token_ids_filter:
            rows_429 = [r for r in rows_429 if str(r.get("access_token_id", "")) in token_ids_filter]

        by_token: dict = {}
        for row in rows_429:
            tid = str(row.get("access_token_id", "unknown"))
            cnt = int(row.get("count", 0))
            by_token[tid] = by_token.get(tid, 0) + cnt

        token_name_map = {str(t["id"]): t["name"] for t in ctx["tokens"]}
        total_429 = sum(by_token.values())

        summary_lines = [f"429 analysis — {window_days}-day window: {total_429} total rate-limit hits"]
        splunk_429_rows = []
        for tid, cnt in sorted(by_token.items(), key=lambda x: -x[1]):
            tname = token_name_map.get(tid, f"id={tid}")
            summary_lines.append(f"  token {tid} ({tname}): {cnt} 429s")
            splunk_429_rows.append({"token_id": tid, "token_name": tname, "count_429": cnt})

        if not total_429:
            summary_lines.append("  No 429 errors found in this window.")

        ep = _build_endpoint_summary(rows_429)
        if ep:
            summary_lines.append(f"Top 429 endpoints: {ep}")

        return (
            "\n".join(summary_lines),
            {
                "type": "step",
                "tool": name,
                "call": f"fetch_429_errors(window_days={window_days})",
                "label": f"429 analysis: {window_days}-day window",
                "detail": f"{total_429} rate-limit hits across {len(by_token)} token(s)",
                "splunk_429_rows": splunk_429_rows,
            },
        )

    if name == "load_recharge_status_codes":
        codes_text = "\n".join(
            f"  {c['code']} {c['name']}: {c['description']}"
            for c in RECHARGE_STATUS_CODES
        )
        return (
            f"ReCharge 2021-11 API status codes:\n{codes_text}",
            {
                "type": "status_codes",
                "codes": RECHARGE_STATUS_CODES,
                "source": "https://developer.rechargepayments.com/2021-11/responses",
            },
        )

    if name == "load_rate_limit_docs":
        return ((SKILLS_DIR / "rate_limit_docs.md").read_text(), {"type": "rate_limit_docs"})

    if name == "load_skill":
        skill = inp.get("skill_name", "")
        if skill not in SKILL_NAMES:
            return f"Unknown skill '{skill}'. Choose one of: {', '.join(SKILL_NAMES)}.", \
                   {"type": "step", "tool": name, "label": f"Unknown skill: {skill}", "detail": ""}
        ctx["skills_loaded"].add(skill)
        criteria = _load_text(SKILLS_DIR / f"{skill}.md")
        return (
            f"Loaded skill '{skill}'. Apply these criteria:\n\n{criteria}",
            {"type": "step", "tool": name, "call": f"load_skill('{skill}')",
             "label": f"Loaded skill: {skill}", "detail": "scoring framework loaded"},
        )

    if name == "score_single_token":
        if not ctx["token_records"]:
            return (
                "No usage data yet. Call fetch_token_usage before scoring.",
                {"type": "step", "tool": name, "label": "Scoring blocked — no usage data", "detail": ""},
            )
        token_id = inp.get("token_id")
        record = ctx["token_records"].get(token_id, {})
        window_days_float = ctx["window_seconds"] / 86400 if ctx["window_seconds"] else 0
        splunk_count = record.get("splunk_count") or 0

        short_window_note = ""
        if not ctx["skills_loaded"]:
            short_window_note += (
                f"\n⚠ No skill loaded yet — scores recorded, but the judge may reject them for lacking criteria backing. "
                f"Consider loading a relevant skill and re-scoring."
            )
        if splunk_count == 0 and 0 < window_days_float < CLEANUP_MIN_WINDOW_DAYS:
            short_window_note += (
                f"\n⚠ Token {token_id} shows 0 calls over only {window_days_float:.0f} days — "
                f"consider re-fetching with a >={CLEANUP_MIN_WINDOW_DAYS}-day window to confirm idleness, "
                f"or emit recommended_action='insufficient_data'."
            )

        is_rescore = token_id in ctx.get("prev_scored", set())
        ctx.setdefault("prev_scored", set()).add(token_id)
        ctx["scored"][token_id] = inp
        ctx["verified"].pop(token_id, None)
        r = inp.get("rotation_score", 0)
        c = inp.get("cleanup_score", 0)
        a = inp.get("security_audit_score", 0)
        return (
            f"Scored token {token_id}: rotation={r}, cleanup={c}, security_audit={a}. "
            f"Call verify_single_token_score({token_id}) to submit for independent audit."
            + short_window_note,
            {"type": "step", "tool": name, "call": f"score_single_token({token_id})",
             "label": f"Scored: {inp.get('token_name', token_id)}",
             "detail": f"rotation={r} | cleanup={c} | audit={a}",
             "rescore": is_rescore},
        )

    if name == "verify_single_token_score":
        token_id = inp.get("token_id")
        score_data = ctx["scored"].get(token_id)
        record = ctx["token_records"].get(token_id)
        if not score_data or not record:
            return (
                f"Cannot verify token {token_id} — score it first.",
                {"type": "step", "tool": name, "label": f"Verify blocked: {token_id} not scored", "detail": ""},
            )
        window_days = round(ctx["window_seconds"] / 86400, 2)
        if ctx.get("auth_mode") == "claude_cli":
            verdict = await _run_judge_cli(record, score_data, window_days)
        else:
            verdict = await _run_judge(ctx["client"], record, score_data, window_days)
        ctx["verified"][token_id] = verdict
        approved = verdict["approved"]
        objections = verdict.get("objections", [])
        result = (
            f"Independent judge {'APPROVED' if approved else 'REJECTED'} token {token_id}.\n"
            + verdict.get("reasoning", "")
            + ("" if approved else
               f"\nObjections: {'; '.join(objections)}\nRe-score to address these, then verify again.")
        )
        return result, {
            "type": "step", "tool": name,
            "call": f"verify_single_token_score({token_id})",
            "label": f"Judge {'APPROVED' if approved else 'REJECTED'}: {token_id}",
            "detail": (verdict.get("reasoning", "") or "")[:200],
            "loop_event": not approved,
        }

    if name == "emit_recommendation":
        token_id = inp.get("token_id")
        verdict = ctx["verified"].get(token_id)
        if not verdict:
            return (
                f"Cannot emit token {token_id} — it has not been verified. "
                f"Call verify_single_token_score({token_id}) first.",
                {"type": "step", "tool": name, "label": f"Emit blocked: {token_id} unverified", "detail": ""},
            )
        if not verdict.get("approved"):
            return (
                f"Cannot emit token {token_id} — the judge REJECTED it. "
                f"Objections: {'; '.join(verdict.get('objections', []))}. "
                f"Re-score, then re-verify before emitting.",
                {"type": "step", "tool": name, "label": f"Emit blocked: {token_id} rejected by judge", "detail": ""},
            )
        score_data = ctx["scored"].get(token_id, {})
        ctx["recommendations"][token_id] = {
            **score_data,
            "recommended_action": inp.get("recommended_action", "no_action"),
            "recommendation": _clean_str(inp.get("recommendation", "")),
            "verification_approved": True,
            "verification_reasoning": verdict.get("reasoning", ""),
        }
        done_count = len(ctx["recommendations"])
        total = ctx["total_tokens"]
        remaining = total - done_count
        return (
            f"Recommendation committed for token {token_id}. {done_count}/{total} finalized. "
            + (f"{remaining} remaining." if remaining > 0
               else "All tokens done. Write a brief store-level summary and stop calling tools."),
            {"type": "step", "tool": name,
             "call": f"emit_recommendation({token_id} → {inp.get('recommended_action', 'no_action')})",
             "label": f"Recommendation: {inp.get('token_name', token_id)} → {inp.get('recommended_action', 'no_action')}",
             "detail": f"{done_count}/{total} done"},
        )

    if name == "record_intent":
        ctx["intent"] = inp
        # Propagate store_id into ctx if the model extracted one and we don't have one yet
        extracted_sid = inp.get("store_id")
        if extracted_sid and not ctx.get("store_id"):
            ctx["store_id"] = extracted_sid

        request_type   = inp.get("request_type", "general_question")
        requires_rec   = inp.get("requires_recommendation", False)
        open_questions = inp.get("open_questions") or []
        token_ids      = inp.get("token_ids_mentioned") or []
        timeframe      = inp.get("timeframe_hint", "")

        label_parts = [f"Understood: {request_type.replace('_', ' ')}"]
        if timeframe:
            label_parts.append(f"timeframe: {timeframe}")
        label = " — ".join(label_parts)

        result_parts = [f"Intent recorded: {request_type}. Requires recommendation: {requires_rec}."]

        if token_ids and ctx.get("conn"):
            # Pre-fetch: Snowflake token lookup + Splunk in parallel
            conn = ctx["conn"]
            first_token_id = token_ids[0]

            _TOKEN_SQL = (
                "SELECT STORE_ID, API_TOKEN_ID AS id, NAME AS name, CREATED_AT "
                "FROM API_TOKEN WHERE API_TOKEN_ID = %s AND _FIVETRAN_DELETED = FALSE LIMIT 1"
            )

            def _fetch_token_record():
                cur = conn.cursor()
                cur.execute(_TOKEN_SQL, (first_token_id,))
                cols = [c[0].lower() for c in cur.description]
                row = cur.fetchone()
                cur.close()
                if not row:
                    return None
                rec = dict(zip(cols, row))
                if rec.get("created_at"):
                    rec["created_at"] = str(rec["created_at"])
                return rec

            # Check TTL cache before hitting Snowflake
            import time as _time
            _cached = _TOKEN_CACHE.get(first_token_id)
            if _cached and (_time.monotonic() - _cached[0]) < TOKEN_CACHE_TTL:
                token_rec = _cached[1]
            else:
                try:
                    token_rec = await asyncio.to_thread(_fetch_token_record)
                    if token_rec:
                        _TOKEN_CACHE[first_token_id] = (_time.monotonic(), token_rec)
                except Exception as e:
                    token_rec = None
                    result_parts.append(f"Snowflake lookup failed: {e}. Will need store_id from user.")

            if token_rec:
                found_store_id = int(token_rec["store_id"])
                ctx["store_id"] = found_store_id
                ctx["tokens"] = [{"id": token_rec["id"], "name": token_rec["name"], "created_at": token_rec["created_at"]}]
                ctx["total_tokens"] = 1

                ctx.setdefault("_pending_sse_events", []).append({
                    "type": "step",
                    "tool": "lookup_token_store",
                    "call": f"lookup_token_store(token_id={first_token_id})",
                    "label": f"Token {first_token_id} → store {found_store_id}",
                    "detail": f"\"{token_rec['name']}\" — resolved from Snowflake",
                })

                # Pre-fetch Splunk + store settings in parallel now that we have store_id
                window_days = 1
                window_secs = window_days * 86400
                try:
                    splunk_res, store_settings = await asyncio.gather(
                        asyncio.to_thread(
                            splunk_client.fetch_store_usage, found_store_id, window_days, "days",
                            token_ids=[first_token_id],
                        ),
                        asyncio.to_thread(_fetch_store_settings_sync, conn, found_store_id),
                        return_exceptions=True,
                    )
                    if isinstance(store_settings, dict):
                        ctx["store_settings"] = store_settings
                        settings_label = _format_store_settings(store_settings).rstrip(".\n")
                        if settings_label:
                            ctx.setdefault("_pending_sse_events", []).append({
                                "type": "step", "tool": "lookup_store_settings",
                                "label": f"Store settings loaded",
                                "detail": settings_label,
                            })
                    if isinstance(splunk_res, Exception):
                        raise splunk_res
                    if splunk_res.get("redirect"):
                        ctx.setdefault("_pending_sse_events", []).append({
                            "type": "step", "tool": "fetch_token_usage",
                            "label": "Splunk pre-fetch blocked — auth required", "detail": "",
                        })
                        result_parts.append("Splunk auth required — no usage data pre-fetched.")
                    else:
                        enriched = _enrich(found_store_id, ctx["tokens"], splunk_res["store_detail_usage"], window_secs)
                        ctx["window_seconds"] = window_secs
                        ctx["max_window_days"] = window_days
                        ctx["fetch_count"] = 1
                        for rec in enriched["tokens"]:
                            ctx["token_records"][rec["id"]] = rec
                        ctx["orphaned"] = enriched.get("orphaned_tokens", [])
                        if not ctx["tokens"]:
                            for orec in ctx["orphaned"]:
                                tid = orec["id"]
                                if tid not in ctx["token_records"]:
                                    ctx["token_records"][int(tid)] = {
                                        "id": int(tid), "name": f"id={tid}",
                                        "splunk_count": orec.get("count", 0),
                                        "calls_per_second": orec.get("calls_per_second", 0),
                                        "fill_pct": {
                                            t: round(orec.get("calls_per_second", 0) / info["leak_rate"] * 100, 1)
                                            for t, info in RATE_TIERS.items()
                                        },
                                        "rate_429": 0, "detail": [],
                                    }
                        summary = _usage_summary(enriched, ctx.get("store_settings"))
                        splunk_rows = [
                            {"id": t["id"], "name": t["name"], "calls": t["splunk_count"],
                             "cps": t["calls_per_second"], "rate_429": t["rate_429"],
                             "fill_top": round(max(t["fill_pct"].values()), 1) if t.get("fill_pct") else 0}
                            for t in enriched["tokens"]
                        ]
                        ctx.setdefault("_pending_sse_events", []).append({
                            "type": "step", "tool": "fetch_token_usage",
                            "call": f"fetch_token_usage(window_days={window_days})",
                            "label": f"Splunk pre-fetched: {window_days}-day window",
                            "detail": f"{enriched['total']} token(s), {len(ctx['orphaned'])} orphaned",
                            "splunk_rows": splunk_rows,
                            "re_query": False,
                        })
                        settings = ctx.get("store_settings", {})
                        settings_note = _format_store_settings(settings)
                        result_parts.append(
                            f"Token {first_token_id} (\"{token_rec['name']}\") is in store {found_store_id}. "
                            f"Splunk data pre-fetched ({window_days}d window). "
                            f"Usage summary:\n{summary}\n"
                            f"{settings_note}"
                            f"This data is already loaded — do NOT call fetch_token_usage or lookup_token_store. "
                            f"Use this data directly to answer the user's request."
                        )
                except Exception as _splunk_err:
                    result_parts.append(
                        f"Splunk pre-fetch failed: {_splunk_err}. "
                        f"Token is in store {found_store_id} — call fetch_token_usage to get usage data."
                    )
            else:
                result_parts.append(
                    f"Token {first_token_id} not found in Snowflake. "
                    f"It may not exist or may have been deleted. Ask the user to verify the token ID."
                )

        elif token_ids:
            # No Snowflake connection
            result_parts.append(
                f"Token IDs specified: {token_ids}. No Snowflake connection available — "
                f"ask the user which store the token belongs to."
            )

        if open_questions:
            # Filter out "no store_id given" when we already resolved it via token lookup
            actionable_qs = [q for q in open_questions if not (token_ids and "store_id" in q.lower())]
            if actionable_qs:
                qs = "; ".join(actionable_qs)
                result_parts.append(f"Open questions that must be resolved before proceeding: {qs}. "
                                     f"Call clarify_with_user now.")

        return (
            " ".join(result_parts),
            {
                "type": "intent",
                "tool": name,
                "request_type": request_type,
                "store_id": extracted_sid,
                "requires_recommendation": requires_rec,
                "open_questions": open_questions,
                "token_ids_mentioned": token_ids,
                "timeframe_hint": timeframe,
                "label": label,
                "detail": f"requires_recommendation={requires_rec}",
            },
        )

    if name == "lookup_store_tokens":
        conn = ctx.get("conn")
        if conn is None:
            return (
                "Snowflake connection not available — cannot look up tokens. "
                "Ask the user to provide the token IDs directly, or check Snowflake credentials.",
                {"type": "step", "tool": name, "label": "Roster lookup blocked — no Snowflake", "detail": ""},
            )
        store_id = int(inp.get("store_id", ctx.get("store_id", 0)))
        if not store_id:
            return (
                "No store_id provided to lookup_store_tokens.",
                {"type": "step", "tool": name, "label": "Roster lookup blocked — no store_id", "detail": ""},
            )

        _SQL = (
            "SELECT API_TOKEN_ID AS id, NAME AS name, CREATED_AT "
            "FROM API_TOKEN "
            "WHERE STORE_ID = %s AND _FIVETRAN_DELETED = FALSE "
            "ORDER BY CREATED_AT DESC"
        )

        def _run_query():
            cur = conn.cursor()
            cur.execute(_SQL, (store_id,))
            cols = [c[0].lower() for c in cur.description]
            rows = cur.fetchall()
            cur.close()
            result = [dict(zip(cols, row)) for row in rows]
            for t in result:
                if t.get("created_at"):
                    t["created_at"] = str(t["created_at"])
            return result

        try:
            tokens, store_settings = await asyncio.gather(
                asyncio.to_thread(_run_query),
                asyncio.to_thread(_fetch_store_settings_sync, conn, store_id),
                return_exceptions=True,
            )
            if isinstance(tokens, Exception):
                raise tokens
            if isinstance(store_settings, dict):
                ctx["store_settings"] = store_settings
        except Exception as e:
            return (
                f"Snowflake query failed: {e}",
                {"type": "step", "tool": name, "label": "Roster lookup error", "detail": str(e)},
            )

        ctx["store_id"]      = store_id
        ctx["tokens"]        = tokens
        ctx["total_tokens"]  = len(tokens)

        roster_lines = "\n".join(
            f"  id={t['id']}, name=\"{t['name']}\", created_at={t['created_at']}"
            for t in tokens
        )
        roster_summary = [{"id": t["id"], "name": t["name"], "created_at": t["created_at"]} for t in tokens]
        settings_note = _format_store_settings(ctx.get("store_settings", {}))

        return (
            f"Store {store_id} has {len(tokens)} token(s):\n{roster_lines}\n{settings_note}",
            {
                "type": "step",
                "tool": name,
                "call": f"lookup_store_tokens(store_id={store_id})",
                "label": f"Roster: {len(tokens)} token(s) for store {store_id}",
                "detail": f"{len(tokens)} token(s) fetched from Snowflake",
                "roster": roster_summary,
            },
        )

    if name == "lookup_token_store":
        conn = ctx.get("conn")
        if conn is None:
            return (
                "Snowflake connection not available — cannot look up token store. "
                "Ask the user which store the token belongs to.",
                {"type": "step", "tool": name, "label": "Token lookup blocked — no Snowflake", "detail": ""},
            )
        token_id = int(inp.get("token_id", 0))
        if not token_id:
            return (
                "No token_id provided to lookup_token_store.",
                {"type": "step", "tool": name, "label": "Token lookup blocked — no token_id", "detail": ""},
            )

        _SQL = (
            "SELECT STORE_ID, API_TOKEN_ID AS id, NAME AS name, CREATED_AT "
            "FROM API_TOKEN "
            "WHERE API_TOKEN_ID = %s AND _FIVETRAN_DELETED = FALSE "
            "LIMIT 1"
        )

        def _run_token_query():
            cur = conn.cursor()
            cur.execute(_SQL, (token_id,))
            cols = [c[0].lower() for c in cur.description]
            row = cur.fetchone()
            cur.close()
            if not row:
                return None
            rec = dict(zip(cols, row))
            if rec.get("created_at"):
                rec["created_at"] = str(rec["created_at"])
            return rec

        try:
            rec = await asyncio.to_thread(_run_token_query)
        except Exception as e:
            return (
                f"Snowflake query failed: {e}",
                {"type": "step", "tool": name, "label": "Token lookup error", "detail": str(e)},
            )

        if rec is None:
            return (
                f"Token {token_id} not found in Snowflake. It may not exist or may have been deleted.",
                {"type": "step", "tool": name, "label": f"Token {token_id} not found", "detail": ""},
            )

        found_store_id = int(rec["store_id"])
        ctx["store_id"] = found_store_id

        return (
            f"Token {token_id} belongs to store {found_store_id} "
            f"(name: \"{rec['name']}\", created_at: {rec['created_at']}). "
            f"Proceed with fetch_token_usage for store {found_store_id}.",
            {
                "type": "step",
                "tool": name,
                "call": f"lookup_token_store(token_id={token_id})",
                "label": f"Token {token_id} → store {found_store_id}",
                "detail": f"\"{rec['name']}\" — resolved from Snowflake",
            },
        )

    return f"Unknown tool: {name}", {"type": "step", "tool": name, "label": f"Unknown tool: {name}", "detail": ""}


# ── Streaming turn ─────────────────────────────────────────────────────────────────

async def _stream_turn(
    client,
    messages: list,
    system_prompt: str,
    tools: list,
    ctx: dict,
    tool_choice: dict = None,
    model: str = None,
) -> AsyncGenerator[str, None]:
    """Stream one LLM turn. Populates ctx['_turn_*']. Yields SSE strings."""
    ctx["_turn_assistant_content"] = []
    ctx["_turn_tool_results"] = []
    ctx["_turn_has_tool_use"] = False
    ctx["_clarification_pending"] = None
    ctx["_turn_error"] = None

    narration_parts: list = []

    use_model = model or MODEL
    use_thinking = use_model != FAST_MODEL  # Haiku doesn't support extended thinking

    try:
        stream_kwargs = dict(
            model=use_model,
            max_tokens=8192 if use_model == FAST_MODEL else 16000,
            system=system_prompt,
            tools=tools,
            tool_choice=tool_choice or {"type": "auto"},
            messages=messages,
        )
        if use_thinking:
            stream_kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

        async with client.messages.stream(**stream_kwargs) as stream:
            # Real-time streaming events
            async for event in stream:
                etype = getattr(event, "type", None)

                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    btype = getattr(block, "type", None) if block else None
                    if btype == "tool_use":
                        ctx["_turn_has_tool_use"] = True

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None) if delta else None

                    if dtype == "thinking_delta":
                        chunk = getattr(delta, "thinking", "") or ""
                        if chunk:
                            yield _sse({"type": "thought_chunk", "delta": chunk, "ts": _ts()})

                    elif dtype == "text_delta":
                        chunk = getattr(delta, "text", "") or ""
                        if chunk:
                            if ctx["_turn_has_tool_use"]:
                                narration_parts.append(chunk)
                            else:
                                yield _sse({"type": "content_chunk", "delta": chunk, "ts": _ts()})

            # Complete message with full content (including thinking signatures)
            final = await stream.get_final_message()

        # Build assistant content from final message
        for block in final.content:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                ctx["_turn_assistant_content"].append({
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                })
            elif btype == "text":
                ctx["_turn_assistant_content"].append({
                    "type": "text",
                    "text": getattr(block, "text", ""),
                })
            elif btype == "tool_use":
                ctx["_turn_assistant_content"].append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        # Dispatch tools
        narration = "".join(narration_parts).strip()[:500]
        first_tool = True
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            if block.name == "clarify_with_user":
                ctx["_clarification_pending"] = {
                    "tool_use_id": block.id,
                    "question": block.input.get("question", ""),
                    "context": block.input.get("context", ""),
                }
                yield _sse({
                    "type": "clarification_request",
                    "question": block.input.get("question", ""),
                    "context": block.input.get("context", ""),
                    "ts": _ts(),
                })
            else:
                result_text, sse_event = await _dispatch_tool(block.name, block.input, ctx)
                if first_tool and narration:
                    sse_event["narration"] = narration
                    first_tool = False
                yield _sse({**sse_event, "ts": _ts()})
                # Drain any bonus events queued by the dispatch (e.g. pre-fetches)
                for extra_event in ctx.pop("_pending_sse_events", []):
                    yield _sse({**extra_event, "ts": _ts()})
                ctx["_turn_tool_results"].append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

    except anthropic.APIError as e:
        yield _sse({"type": "error", "message": f"API error: {e}"})
        ctx["_turn_error"] = str(e)


# ── Token scores builder ──────────────────────────────────────────────────────────

def _build_token_scores(ctx: dict) -> list:
    analyzed_tids = set(ctx.get("scored", {})) | set(ctx.get("recommendations", {}))

    if not ctx.get("tokens"):
        # Free-form path without a roster — build only from what was actually recommended.
        result = []
        for token_id, rec in ctx.get("recommendations", {}).items():
            score = ctx["scored"].get(token_id, {})
            ver = ctx["verified"].get(token_id, {})
            record = ctx["token_records"].get(token_id, {})
            result.append({
                "token_id": token_id,
                "token_name": rec.get("token_name", score.get("token_name", f"id={token_id}")),
                "splunk_count": record.get("splunk_count"),
                "calls_per_second": record.get("calls_per_second"),
                "rate_429": record.get("rate_429"),
                "window_days": round(ctx["window_seconds"] / 86400, 2) if ctx["window_seconds"] else None,
                "rotation_score": score.get("rotation_score", 0),
                "rotation_reasoning": _clean_str(score.get("rotation_reasoning", "")),
                "cleanup_score": score.get("cleanup_score", 0),
                "cleanup_reasoning": _clean_str(score.get("cleanup_reasoning", "")),
                "security_audit_score": score.get("security_audit_score", 0),
                "security_audit_reasoning": _clean_str(score.get("security_audit_reasoning", "")),
                "recommended_action": rec.get("recommended_action", "no_action"),
                "recommendation": rec.get("recommendation", ""),
                "verification_approved": rec.get("verification_approved", True),
                "verification_reasoning": rec.get("verification_reasoning", ""),
            })
        return result
    result = []
    for t in ctx["tokens"]:
        tid = t["id"]
        rec = ctx["recommendations"].get(tid)
        score = ctx["scored"].get(tid, {})
        ver = ctx["verified"].get(tid, {})
        record = ctx["token_records"].get(tid, {})
        base = {
            "token_id": tid,
            "token_name": t["name"],
            "splunk_count": record.get("splunk_count"),
            "calls_per_second": record.get("calls_per_second"),
            "rate_429": record.get("rate_429"),
            "window_days": round(ctx["window_seconds"] / 86400, 2) if ctx["window_seconds"] else None,
        }
        if rec:
            base.update({
                "rotation_score": score.get("rotation_score", 0),
                "rotation_reasoning": _clean_str(score.get("rotation_reasoning", "")),
                "cleanup_score": score.get("cleanup_score", 0),
                "cleanup_reasoning": _clean_str(score.get("cleanup_reasoning", "")),
                "security_audit_score": score.get("security_audit_score", 0),
                "security_audit_reasoning": _clean_str(score.get("security_audit_reasoning", "")),
                "recommended_action": rec.get("recommended_action", "no_action"),
                "recommendation": rec.get("recommendation", ""),
                "verification_approved": rec.get("verification_approved", True),
                "verification_reasoning": rec.get("verification_reasoning", ""),
            })
        elif score:
            scores_map = {
                "token_rotation": score.get("rotation_score", 0),
                "token_cleanup":  score.get("cleanup_score", 0),
                "security_audit": score.get("security_audit_score", 0),
            }
            best = max(scores_map, key=lambda k: scores_map[k])
            base.update({
                "rotation_score": score.get("rotation_score", 0),
                "rotation_reasoning": _clean_str(score.get("rotation_reasoning", "")),
                "cleanup_score": score.get("cleanup_score", 0),
                "cleanup_reasoning": _clean_str(score.get("cleanup_reasoning", "")),
                "security_audit_score": score.get("security_audit_score", 0),
                "security_audit_reasoning": _clean_str(score.get("security_audit_reasoning", "")),
                "recommended_action": best if max(scores_map.values()) >= 20 else "no_action",
                "recommendation": "Analysis incomplete — not emitted.",
                "verification_approved": ver.get("approved", False),
                "verification_reasoning": ver.get("reasoning", ""),
            })
        else:
            # Skip tokens that were never touched when at least one other token was analyzed.
            if analyzed_tids:
                continue
            base.update({
                "rotation_score": 0, "rotation_reasoning": "",
                "cleanup_score": 0, "cleanup_reasoning": "",
                "security_audit_score": 0, "security_audit_reasoning": "",
                "recommended_action": "no_action",
                "recommendation": "Token was not analyzed in this session.",
                "verification_approved": False, "verification_reasoning": "",
            })
        result.append(base)
    return result


# ── Main agentic loop ─────────────────────────────────────────────────────────────

async def run_agent_loop(
    data: dict,
    session: Optional[dict] = None,
    conn=None,
    auth_mode: str = "api_key",
) -> AsyncGenerator[str, None]:
    from memory_store import build_memory_context, save_run

    if auth_mode == "claude_cli":
        client = None
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            yield _sse({"type": "error", "message": "ANTHROPIC_API_KEY not set. Add it to backend/.env."})
            return
        client = anthropic.AsyncAnthropic(api_key=api_key)
    system_prompt = _load_text(SKILLS_DIR / "system_prompt.md")

    tokens = data.get("tokens") or []
    store_id = data.get("store_id") or 0
    user_message = data.get("user_message", "").strip()
    free_form = bool(data.get("free_form"))
    session_id = session["session_id"] if session else None

    if session:
        yield _sse({"type": "session_started", "session_id": session["session_id"], "ts": _ts()})

    yield _sse({"type": "step", "label": "System prompt loaded",
                "detail": "Agent role and rate-limit model initialized.", "ts": _ts()})

    if free_form:
        yield _sse({"type": "step", "label": "Free-form request received",
                    "detail": "Agent will determine scope and fetch what it needs.",
                    "ts": _ts()})
    else:
        yield _sse({"type": "step", "label": "Tokens received (no usage yet)",
                    "detail": f"{len(tokens)} token(s) for store {store_id}. Agent will choose the observation window.",
                    "ts": _ts()})

    # Episodic memory from prior runs (skip for free-form with no store yet)
    memory_context = build_memory_context(store_id) if store_id else ""
    memory_block = f"{memory_context}\n\n" if memory_context else ""

    if free_form:
        # Free-form path: just the user's message; model calls record_intent first
        initial_prompt = (
            f"{memory_block}"
            f"{user_message}\n\n"
            f"Call record_intent first to confirm your understanding, then investigate."
        )
    else:
        token_lines = "\n".join(
            f"  id={t['id']}, name=\"{t['name']}\", created_at={t['created_at']}" for t in tokens
        )
        goal_line = f'User goal: "{user_message}"\n\n' if user_message else ""
        initial_prompt = (
            f"{memory_block}"
            f"{goal_line}"
            f"Store {store_id} — {len(tokens)} API token(s). "
            f"Your objective: ensure every token has a correct, judge-approved recommendation.\n\n"
            f"Tokens:\n{token_lines}\n"
        )

    ctx: dict = {
        "client": client,
        "auth_mode": auth_mode,
        "store_id": store_id,
        "tokens": tokens,
        "total_tokens": len(tokens),
        "window_seconds": 0,
        "max_window_days": 0,
        "token_records": {},
        "orphaned": [],
        "skills_loaded": set(),
        "scored": {},
        "verified": {},
        "recommendations": {},
        "fetch_count": 0,
        "prev_scored": set(),
        "intent": None,
        "conn": conn,
        "store_settings": {},
    }

    if session:
        session["ctx"] = ctx

    messages: list = [{"role": "user", "content": initial_prompt}]
    turn_count = 0

    while turn_count < MAX_TURNS:
        turn_count += 1

        # On turn 1 of a free-form request, force record_intent so the UI always
        # gets the intent card before any data-fetching begins.
        turn_tool_choice = None
        if free_form and turn_count == 1:
            turn_tool_choice = {"type": "tool", "name": "record_intent"}

        if auth_mode == "claude_cli":
            async for event_str in _stream_turn_cli(messages, system_prompt, TOOLS, ctx,
                                                    tool_choice=turn_tool_choice):
                yield event_str
        else:
            # Use FAST_MODEL (Haiku) for structured scoring/verification turns
            turn_model = None
            if turn_count > 1 and ctx.get("token_records"):
                turn_model = FAST_MODEL
            async for event_str in _stream_turn(client, messages, system_prompt, TOOLS, ctx,
                                                tool_choice=turn_tool_choice, model=turn_model):
                yield event_str

        if ctx.get("_turn_error"):
            return

        # Handle HITL clarification pause
        clarification_pending = ctx.get("_clarification_pending")
        if clarification_pending:
            if session:
                session["status"] = "waiting_for_clarification"
                session["clarification_event"].clear()

                elapsed = 0
                timeout = 300
                while not session["clarification_event"].is_set() and elapsed < timeout:
                    await asyncio.sleep(25)
                    elapsed += 25
                    if not session["clarification_event"].is_set():
                        yield _sse({"type": "ping", "ts": _ts()})

                if not session["clarification_event"].is_set():
                    yield _sse({"type": "error", "message": "Clarification timed out after 5 minutes."})
                    return

                answer = session.pop("clarification_answer", "No answer provided.")
                session["clarification_event"].clear()
                session["status"] = "running"
            else:
                answer = "No session available to receive user reply — proceeding with best judgment."

            ctx["_turn_tool_results"].append({
                "type": "tool_result",
                "tool_use_id": clarification_pending["tool_use_id"],
                "content": f"The user replied: {answer}",
            })

        # Advance message history
        messages.append({"role": "assistant", "content": ctx["_turn_assistant_content"]})
        if ctx["_turn_tool_results"]:
            messages.append({"role": "user", "content": ctx["_turn_tool_results"]})

        # Done when model writes prose instead of calling a tool
        if not ctx["_turn_has_tool_use"]:
            final_text = ""
            for block in ctx["_turn_assistant_content"]:
                if block.get("type") == "text":
                    final_text = _clean_str(block.get("text", ""))
                    break

            token_scores = _build_token_scores(ctx)
            all_approved = all(v.get("approved", True) for v in ctx["verified"].values())
            combined_reasoning = "; ".join(
                v.get("reasoning", "") for v in ctx["verified"].values() if v.get("reasoning")
            )[:500]

            # Save episodic memory (use ctx store_id in case it was set mid-loop)
            final_store_id = ctx.get("store_id") or store_id
            token_outcomes = [
                {"token_id": ts["token_id"], "action": ts["recommended_action"]}
                for ts in token_scores
            ]
            if final_store_id:
                save_run(
                    store_id=final_store_id,
                    session_id=session_id or "no-session",
                    store_summary=final_text or "Analysis complete.",
                    token_outcomes=token_outcomes,
                    window_days=ctx["max_window_days"],
                )

            yield _sse({
                "type": "done",
                "session_id": session_id,
                "token_scores": token_scores,
                "store_summary": final_text or "Analysis complete.",
                "iterations": turn_count,
                "approved": all_approved,
                "verification_reasoning": combined_reasoning,
            })
            if session:
                session["status"] = "ready"
                session["messages"] = messages
            return

    token_scores = _build_token_scores(ctx)
    yield _sse({
        "type": "done",
        "session_id": session_id,
        "token_scores": token_scores,
        "store_summary": f"Analysis completed after {turn_count} turns (safety limit reached).",
        "iterations": turn_count,
        "approved": False,
        "verification_reasoning": "Max turns limit reached.",
    })
    if session:
        session["status"] = "ready"
        session["messages"] = messages


# ── Follow-up chat turn ───────────────────────────────────────────────────────────

async def run_chat_turn(
    session: dict,
    user_message: str,
    auth_mode: str = "api_key",
) -> AsyncGenerator[str, None]:
    """Single follow-up turn on an already-completed session."""
    from session_store import update_dialogue

    system_prompt = _load_text(SKILLS_DIR / "system_prompt.md")
    chat_system = (
        system_prompt
        + "\n\nYou are now in follow-up chat mode. Answer the user's question based on the "
        "analysis you just completed. Be concise and cite specific fill percentages and token "
        "IDs from your earlier analysis."
    )

    update_dialogue(session, "human", user_message)
    session["messages"].append({"role": "user", "content": user_message})
    session["status"] = "running"

    if auth_mode == "claude_cli":
        prompt = _build_cli_prompt(chat_system, session["messages"], [])
        try:
            reply_text = await _call_claude_cli(prompt, MODEL)
        except RuntimeError as e:
            session["status"] = "error"
            yield _sse({"type": "error", "message": str(e)})
            return
        reply_text = reply_text.strip()
        yield _sse({"type": "content_chunk", "delta": reply_text, "ts": _ts()})
        session["messages"].append({"role": "assistant", "content": [{"type": "text", "text": reply_text}]})
        update_dialogue(session, "assistant", reply_text)
        session["status"] = "ready"
        yield _sse({"type": "chat_done", "ts": _ts()})
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        yield _sse({"type": "error", "message": "ANTHROPIC_API_KEY not set."})
        return

    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=chat_system,
            messages=session["messages"],
        ) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        chunk = getattr(delta, "text", "") or ""
                        if chunk:
                            yield _sse({"type": "content_chunk", "delta": chunk, "ts": _ts()})

            final = await stream.get_final_message()

        reply_text = ""
        for block in final.content:
            if getattr(block, "type", None) == "text":
                reply_text = getattr(block, "text", "")
                break

        session["messages"].append({"role": "assistant", "content": [{"type": "text", "text": reply_text}]})
        update_dialogue(session, "assistant", reply_text)
        session["status"] = "ready"
        yield _sse({"type": "chat_done", "ts": _ts()})

    except anthropic.APIError as e:
        session["status"] = "error"
        yield _sse({"type": "error", "message": f"API error: {e}"})
