"""Usage enrichment: turn raw Splunk rows into the per-token view the agent reasons about.

Nothing here decides anything — it only shapes data. All decisions live in the
skill files and in the model's turns.
"""

import json as _json
import time as _time
from collections import Counter
from datetime import datetime, timezone

from config import (
    RATE_TIERS,
    STORE_SETTINGS_CACHE_TTL,
    TOKEN_CACHE_TTL,
)

# store_id → (fetched_at_monotonic, settings_dict)
_STORE_SETTINGS_CACHE: dict[int, tuple[float, dict]] = {}
# token_id → (fetched_at_monotonic, record_dict)
_TOKEN_CACHE: dict[int, tuple[float, dict]] = {}


# ── Snowflake sync helpers (called via asyncio.to_thread) ────────────────────────

def fetch_store_settings_sync(conn, store_id: int) -> dict:
    """Fetch rate_limit_multiplier and internal_tokens_limit from store.general_attributes."""
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


_TOKEN_SQL = (
    "SELECT STORE_ID, API_TOKEN_ID AS id, NAME AS name, CREATED_AT "
    "FROM API_TOKEN WHERE API_TOKEN_ID = %s AND _FIVETRAN_DELETED = FALSE LIMIT 1"
)


def fetch_token_record_sync(conn, token_id: int) -> dict:
    """Resolve one token to its store. TTL-cached — the roster rarely changes mid-session."""
    cached = _TOKEN_CACHE.get(token_id)
    if cached and (_time.monotonic() - cached[0]) < TOKEN_CACHE_TTL:
        return cached[1]

    cur = conn.cursor()
    cur.execute(_TOKEN_SQL, (token_id,))
    cols = [c[0].lower() for c in cur.description]
    row = cur.fetchone()
    cur.close()
    if not row:
        return None

    rec = dict(zip(cols, row))
    if rec.get("created_at"):
        rec["created_at"] = str(rec["created_at"])
    _TOKEN_CACHE[token_id] = (_time.monotonic(), rec)
    return rec


# ── Row shaping ─────────────────────────────────────────────────────────────────

def build_detail_by_token(detail_raw: list) -> dict:
    result: dict = {}
    for row in detail_raw:
        tid = str(row.get("access_token_id", ""))
        if not tid:
            continue
        result.setdefault(tid, []).append(row)
    return result


def build_usage_lookup(raw: list) -> dict:
    lookup: dict = {}
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


def build_endpoint_summary(detail_rows: list) -> str:
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


def enrich(store_id: int, tokens: list, detail_raw: list, window_seconds: int) -> dict:
    store_total = build_usage_lookup(detail_raw)
    rate_limit_429: dict = {}
    for row in detail_raw:
        if str(row.get("status_code", "")) == "429":
            tid = str(row.get("access_token_id", ""))
            try:
                cnt = int(row.get("count", 1))  # raw row = 1 request
            except (ValueError, TypeError):
                cnt = 1
            rate_limit_429[tid] = rate_limit_429.get(tid, 0) + cnt

    detail_by_token = build_detail_by_token(detail_raw)
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


def orphan_record(orec: dict) -> dict:
    """Build a token record for an id seen in Splunk but absent from the Snowflake roster."""
    cps = orec.get("calls_per_second", 0)
    return {
        "id": int(orec["id"]),
        "name": f"id={orec['id']}",
        "splunk_count": orec.get("count", 0),
        "calls_per_second": cps,
        "fill_pct": {
            tier: round(cps / info["leak_rate"] * 100, 1)
            for tier, info in RATE_TIERS.items()
        },
        "rate_429": 0,
        "detail": [],
    }


def format_store_settings(settings: dict) -> str:
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


def usage_summary(enriched: dict, store_settings: dict = None) -> str:
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
    actual_leak_rate = None
    if rate_mult:
        try:
            actual_leak_rate = float(rate_mult) * 2  # base rate is 2 calls/s
        except (TypeError, ValueError):
            actual_leak_rate = None

    lines = [
        f"Usage window: {window_days} days ({window}s)",
        f"Store total: {total_calls} calls (known={known_calls}, orphaned={orphaned_calls}), avg {total_cps} calls/s",
    ]
    if actual_leak_rate:
        actual_fill = round(total_cps / actual_leak_rate * 100, 1) if total_calls else 0
        lines.append(
            f"ACTUAL store rate limit (rate_limit_multiplier={rate_mult} → "
            f"leak_rate={actual_leak_rate}/s): store fill = {actual_fill}%"
        )
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
            tier_fill = "fill%=[" + " | ".join(f"{tier}: {fp[tier]}%" for tier in RATE_TIERS) + "]"
            if actual_leak_rate:
                try:
                    token_actual_fill = round(t["calls_per_second"] / actual_leak_rate * 100, 1) if t["calls_per_second"] else 0.0
                    parts.append(f"actual_fill={token_actual_fill}% (multiplier={rate_mult}→{actual_leak_rate}/s)")
                except (TypeError, ValueError):
                    parts.append(tier_fill)
            else:
                parts.append(tier_fill)
        if t["rate_429"]:
            tok_calls = t["splunk_count"] or 0
            ratio_pct = round(t["rate_429"] / tok_calls * 100, 1) if tok_calls else 0
            parts.append(f"rate_limited_429={t['rate_429']} ({ratio_pct}% of calls)")
        if t["other_tokens_with_same_name"]:
            parts.append(f"shares_name_with={t['other_tokens_with_same_name']} other(s)")
        lines.append(", ".join(parts))
        if t.get("detail"):
            ep = build_endpoint_summary(t["detail"])
            if ep:
                lines.append(f"    Endpoints: {ep}")

    if orphaned:
        lines.append("")
        lines.append("ORPHANED (active in Splunk but not in Snowflake):")
        for o in orphaned:
            lines.append(f"  id={o['id']}, calls={o['count']}, calls/s={o['calls_per_second']}")
    return "\n".join(lines)
