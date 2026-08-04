"""Shared Splunk data layer.

Both the HTTP endpoint (`/api/splunk-search`) and the agent's `fetch_token_usage`
tool go through here, so usage data is fetched the SAME way whether a human clicks
"search" or the agent decides — mid-loop — that it needs more data.
"""

import json
import os
from typing import Optional
from urllib.parse import urlencode

import requests as http_requests

WATCHTOWER_URL = "https://recharge-watchtower.infra.rechargeapps.net"
WATCHTOWER_TOKEN_PATH = os.path.expanduser("~/.claude/watchtower-token")

_UNIT_SPL = {"minutes": "m", "hours": "h", "days": "d"}
_UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400}


# ── SPL builders ────────────────────────────────────────────────────────────────

def build_splunk_spl(store_id: int, token_ids: list, time_value: int = 30, time_unit: str = "minutes") -> str:
    tokens_csv = ", ".join(str(t) for t in token_ids)
    earliest = f"-{time_value}{_UNIT_SPL.get(time_unit, 'm')}"
    return (
        f'index=k8s-customcheckout-prod store_id={store_id} '
        f'method IN ("POST","PUT","DELETE","GET") full_path="/*api/*" '
        f'access_token_id IN ({tokens_csv}) earliest={earliest} latest=now\n'
        '| eval request_started_at=(_time-\'request_duration\')\n'
        '| eval request_started_at=strftime(request_started_at, "%Y-%m-%d %H:%M:%S.%Q")\n'
        '| eval request_ended_at=strftime(_time, "%Y-%m-%d %H:%M:%S.%Q")\n'
        '| sort 0 request_started_at\n'
        '| table method full_path status_code request_started_at request_ended_at access_token_id request_duration\n'
        '| stats count by access_token_id'
    )


def build_store_total_spl(store_id: int, time_value: int = 30, time_unit: str = "minutes") -> str:
    earliest = f"-{time_value}{_UNIT_SPL.get(time_unit, 'm')}"
    return (
        f'index=k8s-customcheckout-prod store_id={store_id} '
        f'method IN ("POST","PUT","DELETE","GET") full_path="/*api/*" '
        f'earliest={earliest} latest=now\n'
        '| eval request_started_at=(_time-\'request_duration\')\n'
        '| eval request_started_at=strftime(request_started_at, "%Y-%m-%d %H:%M:%S.%Q")\n'
        '| eval request_ended_at=strftime(_time, "%Y-%m-%d %H:%M:%S.%Q")\n'
        '| sort 0 request_started_at\n'
        '| table method full_path status_code access_token_id\n'
        '| stats count by access_token_id method full_path status_code'
    )


def build_store_total_url(store_id: int, time_value: int = 30, time_unit: str = "minutes") -> str:
    spl = build_store_total_spl(store_id, time_value, time_unit)
    earliest = f"-{time_value}{_UNIT_SPL.get(time_unit, 'm')}"
    params = urlencode({"q": "search " + spl, "earliest": earliest, "latest": "now"})
    return f"https://rechargepayments.splunkcloud.com/en-GB/app/search/search?{params}"


def build_splunk_url(store_id: int, token_ids: list, time_value: int = 30, time_unit: str = "minutes") -> str:
    spl = build_splunk_spl(store_id, token_ids, time_value, time_unit)
    earliest = f"-{time_value}{_UNIT_SPL.get(time_unit, 'm')}"
    params = urlencode({"q": "search " + spl, "earliest": earliest, "latest": "now"})
    return f"https://rechargepayments.splunkcloud.com/en-GB/app/search/search?{params}"


# ── Watchtower / Splunk MCP plumbing ─────────────────────────────────────────────

def load_watchtower_token():
    if not os.path.exists(WATCHTOWER_TOKEN_PATH):
        return None, None
    with open(WATCHTOWER_TOKEN_PATH) as f:
        data = json.load(f)
    return data.get("access_token"), data


def refresh_watchtower_token(token_data) -> Optional[str]:
    refresh_token = token_data.get("refresh_token")
    client_id = token_data.get("client_id")
    client_secret = token_data.get("client_secret")
    if not refresh_token:
        return None
    try:
        resp = http_requests.post(
            f"{WATCHTOWER_URL}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15,
        )
        if not resp.ok:
            return None
        tokens = resp.json()
        new_token = tokens.get("access_token")
        if new_token:
            token_data["access_token"] = new_token
            if tokens.get("refresh_token"):
                token_data["refresh_token"] = tokens["refresh_token"]
            with open(WATCHTOWER_TOKEN_PATH, "w") as f:
                json.dump(token_data, f)
            os.chmod(WATCHTOWER_TOKEN_PATH, 0o600)
        return new_token
    except Exception:
        return None


def call_watchtower_splunk(query: str, access_token: str):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    resp = http_requests.post(
        f"{WATCHTOWER_URL}/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": {
            "name": "splunk_search",
            "arguments": {"query": query},
        }},
        timeout=90,
        stream=True,
    )
    if resp.status_code == 401:
        return None  # signal token expired
    resp.raise_for_status()

    raw = ""
    for chunk in resp.iter_content(chunk_size=None):
        if chunk:
            raw += chunk.decode("utf-8", errors="replace")

    rpc = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            rpc = json.loads(line[5:].strip())
            break

    if rpc is None:
        raise ValueError(f"No SSE data line in response: {raw[:300]}")
    if "error" in rpc:
        raise ValueError(f"Watchtower RPC error: {rpc['error']}")

    result = rpc.get("result", {})
    if result.get("isError"):
        text = (result.get("content") or [{}])[0].get("text", "unknown error")
        raise ValueError(f"Splunk error: {text}")

    text = (result.get("content") or [{}])[0].get("text", "{}")
    return json.loads(text)


def extract_detail_usage(result: dict) -> list:
    """Return full detail rows (access_token_id, method, full_path, status_code, count)."""
    return [
        {
            "access_token_id": str(row.get("access_token_id", "")),
            "method":          str(row.get("method", "")),
            "full_path":       str(row.get("full_path", "")),
            "status_code":     str(row.get("status_code", "")),
            "count":           str(row.get("count", "0")),
        }
        for row in result.get("results", [])
        if row.get("access_token_id")
    ]


def extract_store_total_usage(result: dict) -> list:
    """Aggregate detail rows to per-token counts."""
    agg = {}
    for row in result.get("results", []):
        tid = str(row.get("access_token_id", ""))
        if not tid:
            continue
        try:
            agg[tid] = agg.get(tid, 0) + int(row.get("count", 0))
        except (ValueError, TypeError):
            pass
    return [{"access_token_id": tid, "count": str(cnt)} for tid, cnt in agg.items()]


# ── Unified fetch (live) ─────────────────────────────────────────────────────────

def fetch_store_usage(store_id: int, time_value: int, time_unit: str) -> dict:
    """Fetch store-total usage from Splunk (live). Returns a dict with either
    usage rows or a `redirect` flag when Watchtower auth is required."""
    splunk_url = build_store_total_url(store_id, time_value, time_unit)
    query = build_store_total_spl(store_id, time_value, time_unit)

    access_token, token_data = load_watchtower_token()
    if not access_token:
        return {"redirect": True, "splunk_url": splunk_url,
                "store_detail_usage": [], "store_total_usage": [], "columns": [], "rows": [], "total": 0}

    try:
        result = call_watchtower_splunk(query, access_token)
        if result is None:
            access_token = refresh_watchtower_token(token_data)
            if not access_token:
                return {"redirect": True, "splunk_url": splunk_url,
                        "store_detail_usage": [], "store_total_usage": [], "columns": [], "rows": [], "total": 0}
            result = call_watchtower_splunk(query, access_token)
            if result is None:
                return {"redirect": True, "splunk_url": splunk_url,
                        "store_detail_usage": [], "store_total_usage": [], "columns": [], "rows": [], "total": 0}
    except http_requests.exceptions.RequestException as e:
        raise ValueError(f"Watchtower unreachable: {e}")

    detail_usage = extract_detail_usage(result)
    store_total_usage = extract_store_total_usage(result)
    results_list = result.get("results", [])
    columns = list(results_list[0].keys()) if results_list else []
    rows = [[str(row.get(col, "")) for col in columns] for row in results_list]

    return {
        "redirect": False,
        "splunk_url": splunk_url,
        "store_detail_usage": detail_usage,
        "store_total_usage": store_total_usage,
        "columns": columns,
        "rows": rows,
        "total": result.get("count", len(results_list)),
    }


def window_seconds(time_value: int, time_unit: str) -> int:
    return time_value * _UNIT_SECONDS.get(time_unit, 60)
