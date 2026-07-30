"""Per-store episodic memory — JSON files in backend/memory/."""

import json
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path(__file__).parent / "memory"
MAX_RUNS = 5


def _path(store_id: int) -> Path:
    MEMORY_DIR.mkdir(exist_ok=True)
    return MEMORY_DIR / f"{store_id}.json"


def save_run(
    store_id: int,
    session_id: str,
    store_summary: str,
    token_outcomes: list,
    window_days: float,
) -> None:
    path = _path(store_id)
    runs: list = []
    if path.exists():
        try:
            runs = json.loads(path.read_text())
        except Exception:
            runs = []
    runs.append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "session_id": session_id,
        "store_summary": store_summary[:300],
        "window_days": window_days,
        "token_outcomes": token_outcomes,
    })
    path.write_text(json.dumps(runs[-MAX_RUNS:], indent=2))


def build_memory_context(store_id: int) -> str:
    path = _path(store_id)
    if not path.exists():
        return ""
    try:
        runs = json.loads(path.read_text())
    except Exception:
        return ""
    if not runs:
        return ""

    lines = [f"Prior audit history for store {store_id} ({len(runs)} run(s)):"]
    for r in runs:
        date = r.get("date", "unknown")
        summary = r.get("store_summary", "")
        outcomes = r.get("token_outcomes", [])
        actions = ", ".join(
            f"token {o.get('token_id')} → {o.get('action')}"
            for o in outcomes
            if o.get("action") not in (None, "no_action")
        )
        lines.append(f"  [{date}] {summary}")
        if actions:
            lines.append(f"    Actions: {actions}")
    lines.append("---")
    return "\n".join(lines)
