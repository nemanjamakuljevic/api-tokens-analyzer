"""Agentic loop for API-token analysis.

The model — not Python — drives every decision:

  fetch_token_usage   the agent picks the observation window and pulls Splunk usage.
                      It has NO usage data until it calls this, and it may re-query a
                      longer window when what it finds is inconclusive (e.g. a token
                      looks idle but the window is < 30 days).
  load_skill          the agent reads a token's usage, then decides which scoring
                      framework applies and loads only that one. Skills are NOT all
                      pre-loaded — selection is the agent's call.
  score_single_token  records a 0–100 score per candidate action.
  verify_single_token_score
                      hands the score to an INDEPENDENT judge (a separate LLM call
                      with an adversarial prompt). The judge can reject; a rejected
                      token cannot be emitted until it is re-scored and re-approved.
  emit_recommendation finalizes — gated on a passing verdict.

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
MODEL = "claude-sonnet-5"  # Extended thinking requires Sonnet+
JUDGE_MODEL = "claude-sonnet-5"

DEFAULT_WINDOW_SECONDS = 604_800  # 7 days
CLEANUP_MIN_WINDOW_DAYS = 30

RATE_TIERS = {
    "nonpro_1x1": {"leak_rate": 2,  "bucket": 40},
    "nonpro_2x1": {"leak_rate": 4,  "bucket": 40},
    "pro_2x2":    {"leak_rate": 4,  "bucket": 80},
    "pro_5x3":    {"leak_rate": 10, "bucket": 120},
    "pro_10x3":   {"leak_rate": 20, "bucket": 120},
}

MAX_TURNS = 60  # Safety cap — model drives, Python caps runaway

# ── Tools the agent decides to call ──────────────────────────────────────────────

FETCH_TOKEN_USAGE_TOOL = {
    "name": "fetch_token_usage",
    "description": (
        "Pull Splunk API usage for THIS store over an observation window you choose. "
        "You start with no usage data — call this first. Returns per-token call counts, "
        "average calls/s, rate-limit fill %, HTTP 429 counts, and endpoint breakdown."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "description": "Observation window in days, as specified in the task.",
            },
            "reason": {
                "type": "string",
                "description": "Brief note about what you expect to find.",
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

TOOLS = [
    FETCH_TOKEN_USAGE_TOOL,
    LOAD_SKILL_TOOL,
    SCORE_SINGLE_TOKEN_TOOL,
    VERIFY_SINGLE_TOKEN_TOOL,
    EMIT_RECOMMENDATION_TOOL,
]

# Forced tool for the independent judge
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
    endpoint_counts = {}
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


# ── Enrichment (runs per fetch, over the window the agent chose) ──────────────────

def _enrich(store_id: int, tokens: list, detail_raw: list, window_seconds: int) -> dict:
    store_total = _build_usage_lookup(detail_raw)
    usage = store_total
    has_usage_source = True  # a fetch happened, so 0 means "definitively 0"

    rate_limit_429 = {}
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
        for tid, cnt in usage.items()
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
        splunk_count = usage.get(tid_str, 0) if has_usage_source else None

        if splunk_count is not None:
            calls_per_second = round(splunk_count / window_seconds, 4)
            fill_pct = {
                tier: round(calls_per_second / info["leak_rate"] * 100, 1)
                for tier, info in RATE_TIERS.items()
            }
        else:
            calls_per_second = None
            fill_pct = None

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


# ── Independent judge (a separate LLM call, adversarial prompt) ──────────────────

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
    # Fail-open so a judge outage never deadlocks the loop
    return {"approved": True, "objections": [], "reasoning": "Judge unavailable — auto-approved (fail-open)."}


# ── Tool dispatch ────────────────────────────────────────────────────────────────

async def _dispatch_tool(name, inp, ctx):
    """Execute one tool call. ctx holds all mutable loop state. Returns (result_text, sse_event)."""
    if name == "fetch_token_usage":
        window_days = int(inp.get("window_days", 7))
        window_secs = window_days * 86400
        if ctx["demo"]:
            usage = splunk_client.demo_store_usage(ctx["store_id"], window_secs)
        else:
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

        enriched = _enrich(ctx["store_id"], ctx["tokens"], usage["store_detail_usage"], window_secs)
        ctx["window_seconds"] = window_secs
        ctx["max_window_days"] = max(ctx["max_window_days"], window_days)
        for rec in enriched["tokens"]:
            ctx["token_records"][rec["id"]] = rec
        ctx["orphaned"] = enriched.get("orphaned_tokens", [])
        summary = _usage_summary(enriched)
        return (
            f"Fetched usage over a {window_days}-day window.\n\n{summary}\n\n"
            f"Now decide, per token: which skill applies → load_skill → score → verify → emit.",
            {"type": "step", "tool": name,
             "call": f"fetch_token_usage(window_days={window_days})",
             "label": f"Fetched usage: {window_days}-day window",
             "detail": inp.get("reason", f"{enriched['total']} tokens, {len(ctx['orphaned'])} orphaned")},
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
                "No skill loaded yet. Call load_skill for the framework(s) relevant to this "
                "token before scoring.",
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

        # When a token has 0 calls and the window is too short, the agent must
        # emit 'insufficient_data' — not try to re-fetch a longer window.
        short_window_note = ""
        if splunk_count == 0 and 0 < window_days_float < CLEANUP_MIN_WINDOW_DAYS:
            short_window_note = (
                f"\n⚠ Token {token_id} shows 0 calls over only {window_days_float:.0f} days — "
                f"this window is too short to confirm idleness. "
                f"Set all scores to 0 and emit recommended_action='insufficient_data' "
                f"with a note that the user should retry the Splunk search with a "
                f"minimum {CLEANUP_MIN_WINDOW_DAYS}-day window."
            )

        ctx["scored"][token_id] = inp
        ctx["verified"].pop(token_id, None)  # a re-score invalidates any prior verdict
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


def _build_token_scores(ctx) -> list:
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


# ── Main agentic loop ────────────────────────────────────────────────────────────

async def run_agent_loop(data: dict) -> AsyncGenerator[str, None]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        yield _sse({"type": "error", "message": "ANTHROPIC_API_KEY not set. Add it to backend/.env."})
        return

    client = anthropic.AsyncAnthropic(api_key=api_key)
    system_prompt = _load_text(SKILLS_DIR / "system_prompt.md")

    tokens = data["tokens"]
    store_id = data["store_id"]
    demo = bool(data.get("demo", False))
    suggested_days = max(1, round(data.get("time_window_seconds", DEFAULT_WINDOW_SECONDS) / 86400))

    yield _sse({"type": "step", "label": "System prompt loaded",
                "detail": "Agent role and rate-limit model initialized.", "ts": _ts()})
    yield _sse({"type": "step", "label": "Tokens received (no usage yet)",
                "detail": f"{len(tokens)} token(s) for store {store_id}. Agent must fetch usage itself.",
                "ts": _ts()})

    token_lines = "\n".join(
        f"  id={t['id']}, name=\"{t['name']}\", created_at={t['created_at']}" for t in tokens
    )
    skill_menu = (
        "  token_rotation — refresh/redistribute active or rate-limited tokens; stalled migrations\n"
        "  token_cleanup  — revoke tokens idle over a ≥30-day window\n"
        "  security_audit — investigate fill % over capacity, 429s, or anomalous spikes"
    )

    initial_prompt = (
        f"Store {store_id} has {len(tokens)} API token(s) (from Snowflake). You have NO usage "
        f"data yet.\n\nTokens:\n{token_lines}\n\n"
        f"Skills you can load (choose per token — do not assume all apply):\n{skill_menu}\n\n"
        f"Work the problem:\n"
        f"1. Call fetch_token_usage(window_days={suggested_days}) — use exactly this window as requested.\n"
        f"2. For EACH token: decide which skill(s) apply from what the data shows, load_skill, "
        f"score_single_token, verify_single_token_score (independent audit), then emit_recommendation.\n"
        f"3. If a token shows 0 calls and your window is < {CLEANUP_MIN_WINDOW_DAYS} days: "
        f"emit recommended_action='insufficient_data'. The recommendation text must tell "
        f"the user to retry the Splunk search with a minimum {CLEANUP_MIN_WINDOW_DAYS}-day window. "
        f"Do NOT re-fetch a longer window — leave that to the user.\n"
        f"4. A token can only be emitted after the judge approves it. If rejected, re-score and re-verify.\n"
        f"5. When every token has an emitted recommendation, write a brief store-level summary and stop.\n\n"
        f"Before EACH tool call, write one short sentence stating the decision you are making and why "
        f"(e.g. 'Token 1152453 looks idle but the 7-day window is too short, so I'll re-fetch 30 days'). "
        f"This narration is shown live to the user as your reasoning.\n\n"
        f"Rules: score each token on its OWN data; cite fill percentages, not raw calls/s; "
        f"**bold token IDs/names** in recommendations; any rate_429 > 0 means rate limiting is occurring."
    )

    ctx = {
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

    messages = [{"role": "user", "content": initial_prompt}]
    turn_count = 0

    while turn_count < MAX_TURNS:
        turn_count += 1
        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=system_prompt,
                tools=TOOLS,
                tool_choice={"type": "auto"},
                messages=messages,
            )
        except anthropic.APIError as e:
            yield _sse({"type": "error", "message": f"API error on turn {turn_count}: {e}"})
            return

        has_tool_use = any(getattr(b, "type", None) == "tool_use" for b in response.content)

        # Extended thinking blocks (rare with adaptive on Sonnet 5) — surface separately.
        for block in response.content:
            if getattr(block, "type", None) == "thinking":
                snippet = (getattr(block, "thinking", "") or "").strip()[:800]
                if snippet:
                    yield _sse({"type": "thinking", "subtype": "extended",
                                "content": snippet, "ts": _ts()})

        # Capture the per-turn narration the agent writes before its tool call.
        # Rather than emitting it as a standalone event, we attach it to the first
        # tool step so the panel shows "why" inline with "what".
        narration_parts = []
        for block in response.content:
            if getattr(block, "type", None) == "text" and has_tool_use:
                text = (getattr(block, "text", "") or "").strip()
                if text:
                    narration_parts.append(text[:400])
        turn_narration = " ".join(narration_parts).strip()[:500]

        tool_results = []
        first_tool = True
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            result_text, sse_event = await _dispatch_tool(block.name, block.input, ctx)
            if first_tool and turn_narration:
                sse_event["narration"] = turn_narration
                first_tool = False
            yield _sse({**sse_event, "ts": _ts()})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        assistant_content = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                assistant_content.append({
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                })
            elif btype == "text":
                assistant_content.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "tool_use":
                assistant_content.append({
                    "type": "tool_use", "id": block.id, "name": block.name, "input": block.input,
                })
        messages.append({"role": "assistant", "content": assistant_content})
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if not has_tool_use:
            final_text = ""
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    final_text = _clean_str(getattr(block, "text", ""))
                    break
            token_scores = _build_token_scores(ctx)
            all_approved = all(v.get("approved", True) for v in ctx["verified"].values())
            combined_reasoning = "; ".join(
                v.get("reasoning", "") for v in ctx["verified"].values() if v.get("reasoning")
            )[:500]
            yield _sse({
                "type": "done",
                "token_scores": token_scores,
                "store_summary": final_text or "Analysis complete.",
                "iterations": turn_count,
                "approved": all_approved,
                "verification_reasoning": combined_reasoning,
            })
            return

    token_scores = _build_token_scores(ctx)
    yield _sse({
        "type": "done",
        "token_scores": token_scores,
        "store_summary": f"Analysis completed after {turn_count} turns (safety limit reached).",
        "iterations": turn_count,
        "approved": False,
        "verification_reasoning": "Max turns limit reached.",
    })
