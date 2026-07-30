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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

import anthropic

import splunk_client

SKILLS_DIR = Path(__file__).parent / "skills"
SKILL_NAMES = ["token_rotation", "token_cleanup", "security_audit"]
MODEL = "claude-sonnet-5"
JUDGE_MODEL = "claude-sonnet-5"

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
        "Pull Splunk API usage for THIS store over an observation window you choose. "
        "You start with no usage data — call this first with a 1-day window. "
        "Extend to ≥30 days ONLY for cleanup verification — and ONLY after calling "
        "clarify_with_user to confirm the user wants the wider query. "
        "Returns per-token call counts, avg calls/s, rate-limit fill %, HTTP 429 counts, "
        "and endpoint breakdown."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "description": "Observation window in days. Default 1 day. Extend to ≥30 only for cleanup — ask user first via clarify_with_user.",
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
        "token_rotation: active/rate-limited tokens or stalled migrations. "
        "token_cleanup: tokens idle over a ≥30-day window. "
        "security_audit: fill % over capacity, 429s, or anomalous spikes. "
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
        "Pause the analysis and ask the user a clarifying question. Use when the data "
        "is genuinely ambiguous and the user's context would change your recommendation "
        "(e.g. 'Is this migration still in progress?', 'Are these duplicate tokens "
        "intentional?', 'What timeframe should I focus on?'). "
        "Do not ask questions you can answer from the data alone."
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
    FETCH_TOKEN_USAGE_TOOL,
    FETCH_429_ERRORS_TOOL,
    LOAD_SKILL_TOOL,
    LOAD_RECHARGE_STATUS_CODES_TOOL,
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
            cnt = int(row.get("count", 0))
        except (ValueError, TypeError):
            cnt = 0
        lookup[tid] = lookup.get(tid, 0) + cnt
    return lookup


def _build_endpoint_summary(detail_rows: list) -> str:
    endpoint_counts: dict = {}
    for row in detail_rows:
        method = row.get("method", "")
        path = row.get("full_path", "")
        status = str(row.get("status_code", ""))
        try:
            cnt = int(row.get("count", 0))
        except (ValueError, TypeError):
            cnt = 0
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
                cnt = int(row.get("count", 0))
            except (ValueError, TypeError):
                cnt = 0
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


def _usage_summary(enriched: dict) -> str:
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

    lines = [
        f"Usage window: {window_days} days ({window}s)",
        f"Store total: {total_calls} calls (known={known_calls}, orphaned={orphaned_calls}), avg {total_cps} calls/s",
        f"Store fill % by tier: {store_fill}",
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
        if ctx["demo"]:
            usage = splunk_client.demo_store_usage(ctx["store_id"], window_secs)
        else:
            try:
                res = await asyncio.to_thread(
                    splunk_client.fetch_store_usage, ctx["store_id"], window_days, "days"
                )
                if res.get("redirect"):
                    return (
                        "Splunk authentication required — cannot fetch live usage. "
                        "Auth in the Splunk tab, or run in demo mode.",
                        {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required",
                         "detail": res.get("splunk_url", "")},
                    )
                usage = {"store_detail_usage": res["store_detail_usage"]}
            except (ValueError, Exception) as splunk_err:
                usage = splunk_client.demo_store_usage(ctx["store_id"], window_secs)
                ctx["demo"] = True

        enriched = _enrich(ctx["store_id"], ctx["tokens"], usage["store_detail_usage"], window_secs)
        ctx["window_seconds"] = window_secs
        ctx["max_window_days"] = max(ctx["max_window_days"], window_days)
        for rec in enriched["tokens"]:
            ctx["token_records"][rec["id"]] = rec
        ctx["orphaned"] = enriched.get("orphaned_tokens", [])
        summary = _usage_summary(enriched)
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
            f"Fetched usage over a {window_days}-day window.\n\n{summary}\n\n"
            f"Now decide, per token: which skill applies → load_skill → score → verify → emit.",
            {"type": "step", "tool": name,
             "call": f"fetch_token_usage(window_days={window_days})",
             "label": f"Fetched usage: {window_days}-day window",
             "detail": inp.get("reason", f"{enriched['total']} tokens, {len(ctx['orphaned'])} orphaned"),
             "splunk_rows": splunk_rows},
        )

    if name == "fetch_429_errors":
        window_days = int(inp.get("window_days", DEFAULT_WINDOW_DAYS))
        window_secs = window_days * 86400
        token_ids_filter = {str(i) for i in inp.get("token_ids", [])}

        if ctx["demo"]:
            raw = splunk_client.demo_store_usage(ctx["store_id"], window_secs)["store_detail_usage"]
        else:
            try:
                res = await asyncio.to_thread(
                    splunk_client.fetch_store_usage, ctx["store_id"], window_days, "days"
                )
                if res.get("redirect"):
                    return (
                        "Splunk authentication required.",
                        {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required", "detail": ""},
                    )
                raw = res["store_detail_usage"]
            except Exception:
                raw = splunk_client.demo_store_usage(ctx["store_id"], window_secs)["store_detail_usage"]
                ctx["demo"] = True

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
        if not ctx["skills_loaded"]:
            return (
                "No skill loaded yet. Call load_skill for the framework(s) relevant to this token before scoring.",
                {"type": "step", "tool": name, "label": "Scoring blocked — load a skill first", "detail": ""},
            )
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
        if splunk_count == 0 and 0 < window_days_float < CLEANUP_MIN_WINDOW_DAYS:
            short_window_note = (
                f"\n⚠ Token {token_id} shows 0 calls over only {window_days_float:.0f} days — "
                f"consider re-fetching with a ≥{CLEANUP_MIN_WINDOW_DAYS}-day window to confirm idleness, "
                f"or emit recommended_action='insufficient_data'."
            )

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
             "detail": f"rotation={r} | cleanup={c} | audit={a}"},
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

    return f"Unknown tool: {name}", {"type": "step", "tool": name, "label": f"Unknown tool: {name}", "detail": ""}


# ── Streaming turn ─────────────────────────────────────────────────────────────────

async def _stream_turn(
    client,
    messages: list,
    system_prompt: str,
    tools: list,
    ctx: dict,
) -> AsyncGenerator[str, None]:
    """Stream one LLM turn. Populates ctx['_turn_*']. Yields SSE strings."""
    ctx["_turn_assistant_content"] = []
    ctx["_turn_tool_results"] = []
    ctx["_turn_has_tool_use"] = False
    ctx["_clarification_pending"] = None
    ctx["_turn_error"] = None

    narration_parts: list = []

    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive", "display": "summarized"},
            system=system_prompt,
            tools=tools,
            tool_choice={"type": "auto"},
            messages=messages,
        ) as stream:
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
) -> AsyncGenerator[str, None]:
    from memory_store import build_memory_context, save_run

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        yield _sse({"type": "error", "message": "ANTHROPIC_API_KEY not set. Add it to backend/.env."})
        return

    client = anthropic.AsyncAnthropic(api_key=api_key)
    system_prompt = _load_text(SKILLS_DIR / "system_prompt.md")

    tokens = data["tokens"]
    store_id = data["store_id"]
    demo = bool(data.get("demo", False))
    user_message = data.get("user_message", "").strip()
    session_id = session["session_id"] if session else None

    if session:
        yield _sse({"type": "session_started", "session_id": session["session_id"], "ts": _ts()})

    yield _sse({"type": "step", "label": "System prompt loaded",
                "detail": "Agent role and rate-limit model initialized.", "ts": _ts()})
    yield _sse({"type": "step", "label": "Tokens received (no usage yet)",
                "detail": f"{len(tokens)} token(s) for store {store_id}. Agent will choose the observation window.",
                "ts": _ts()})

    token_lines = "\n".join(
        f"  id={t['id']}, name=\"{t['name']}\", created_at={t['created_at']}" for t in tokens
    )

    # Episodic memory from prior runs
    memory_context = build_memory_context(store_id)
    memory_block = f"{memory_context}\n\n" if memory_context else ""

    # Objective-driven prompt — no prescriptive steps, model decides everything
    goal_line = f'User goal: "{user_message}"\n\n' if user_message else ""
    initial_prompt = (
        f"{memory_block}"
        f"{goal_line}"
        f"Store {store_id} — {len(tokens)} API token(s). "
        f"Your objective: ensure every token has a correct, judge-approved recommendation.\n\n"
        f"Tokens:\n{token_lines}\n\n"
        f"Available skills: token_rotation · token_cleanup · security_audit\n"
        f"Extra tools: fetch_429_errors (deep 429 analysis), load_recharge_status_codes (API error reference)\n\n"
        f"Hard constraints:\n"
        f"- You have no usage data until you call fetch_token_usage. Start with 1 day. "
        f"Before re-fetching with a ≥{CLEANUP_MIN_WINDOW_DAYS}-day window (e.g. to confirm idleness for cleanup), "
        f"call clarify_with_user first to confirm the user wants a wider query.\n"
        f"- A token with 0 calls and a confirmed ≥{CLEANUP_MIN_WINDOW_DAYS}-day window → token_cleanup. "
        f"A token with 0 calls on a window < {CLEANUP_MIN_WINDOW_DAYS} days → recommended_action='insufficient_data'.\n"
        f"- A token can only be emitted after the judge approves. If rejected, re-score and re-verify.\n"
        f"- Use clarify_with_user if context from the user would materially change your recommendation.\n"
        f"- Before each tool call, write one sentence stating your decision and why.\n"
        f"- Cite fill percentages, not raw calls/s. **Bold** token IDs/names in recommendations.\n"
    )

    ctx: dict = {
        "client": client,
        "store_id": store_id,
        "tokens": tokens,
        "demo": demo,
        "total_tokens": len(tokens),
        "window_seconds": 0,
        "max_window_days": 0,
        "token_records": {},
        "orphaned": [],
        "skills_loaded": set(),
        "scored": {},
        "verified": {},
        "recommendations": {},
    }

    if session:
        session["ctx"] = ctx

    messages: list = [{"role": "user", "content": initial_prompt}]
    turn_count = 0

    while turn_count < MAX_TURNS:
        turn_count += 1

        async for event_str in _stream_turn(client, messages, system_prompt, TOOLS, ctx):
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

            # Save episodic memory
            token_outcomes = [
                {"token_id": ts["token_id"], "action": ts["recommended_action"]}
                for ts in token_scores
            ]
            save_run(
                store_id=store_id,
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
) -> AsyncGenerator[str, None]:
    """Single follow-up turn on an already-completed session."""
    from session_store import update_dialogue

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        yield _sse({"type": "error", "message": "ANTHROPIC_API_KEY not set."})
        return

    client = anthropic.AsyncAnthropic(api_key=api_key)
    system_prompt = _load_text(SKILLS_DIR / "system_prompt.md")

    update_dialogue(session, "human", user_message)
    session["messages"].append({"role": "user", "content": user_message})
    session["status"] = "running"

    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt + "\n\nYou are now in follow-up chat mode. Answer the user's question based on the analysis you just completed. Be concise and cite specific fill percentages and token IDs from your earlier analysis.",
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
