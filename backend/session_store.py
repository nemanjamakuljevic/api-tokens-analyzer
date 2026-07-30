"""In-memory session registry with TTL reaper.

Single-process only — asyncio.Event is not cross-process.
Run uvicorn with --workers 1.
"""

import asyncio
import uuid
from typing import Optional


_sessions: dict[str, dict] = {}


def create_session(store_id: int) -> dict:
    loop = asyncio.get_event_loop()
    sid = str(uuid.uuid4())
    session: dict = {
        "session_id": sid,
        "store_id": store_id,
        "status": "running",
        "messages": [],
        "dialogue": [],
        "ctx": {},
        "clarification_event": asyncio.Event(),
        "clarification_answer": None,
        "created_at": loop.time(),
        "last_active": loop.time(),
    }
    _sessions[sid] = session
    return session


def get_session(session_id: str) -> Optional[dict]:
    return _sessions.get(session_id)


def touch_session(session_id: str) -> None:
    s = _sessions.get(session_id)
    if s:
        s["last_active"] = asyncio.get_event_loop().time()


def update_dialogue(session: Optional[dict], role: str, text: str) -> None:
    if session is None or not text:
        return
    session["dialogue"].append({"role": role, "text": text})


async def reaper_task(ttl_seconds: int = 3600) -> None:
    while True:
        await asyncio.sleep(60)
        now = asyncio.get_event_loop().time()
        dead = [
            sid for sid, s in list(_sessions.items())
            if now - s["last_active"] > ttl_seconds
        ]
        for sid in dead:
            _sessions.pop(sid, None)
