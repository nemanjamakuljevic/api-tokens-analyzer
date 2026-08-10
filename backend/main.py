import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
import snowflake.connector
from dotenv import load_dotenv
from typing import Optional
import os

from agent import run_agent_loop, run_chat_turn
import splunk_client
import session_store

load_dotenv(override=True)

_conn: Optional[snowflake.connector.SnowflakeConnection] = None


def credentials_configured() -> bool:
    return bool(os.environ.get("SNOWFLAKE_ACCOUNT") and os.environ.get("SNOWFLAKE_USER"))


def create_connection() -> snowflake.connector.SnowflakeConnection:
    password = os.environ.get("SNOWFLAKE_PASSWORD")
    conn_kwargs: dict = dict(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "ODS"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "CURATED"),
    )
    if password:
        conn_kwargs["password"] = password
    else:
        conn_kwargs["authenticator"] = "externalbrowser"
    return snowflake.connector.connect(**conn_kwargs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conn
    if credentials_configured():
        print("Snowflake credentials found — connecting in background (browser auth may open)...")
        async def _connect_bg():
            global _conn
            try:
                _conn = await asyncio.to_thread(create_connection)
                print("Snowflake connection established — live mode active.")
            except Exception as e:
                print(f"Snowflake connection failed: {e} — live token lookup unavailable.")
        asyncio.create_task(_connect_bg())
    else:
        print("No Snowflake credentials — /api/tokens will return 503.")
    reaper = asyncio.create_task(session_store.reaper_task())
    yield
    reaper.cancel()
    if _conn:
        _conn.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TokensRequest(BaseModel):
    store_id: int

    @field_validator("store_id")
    @classmethod
    def store_id_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("store_id must be a positive integer")
        return v


class SplunkSearchRequest(BaseModel):
    store_id: int
    token_ids: list[int] = []
    time_value: int = 30
    time_unit: str = "days"


class TokenItem(BaseModel):
    id: int
    name: str
    created_at: str


class AnalyzeRequest(BaseModel):
    store_id: int
    tokens: list[TokenItem]
    time_window_seconds: int = 604800


class ChatStartRequest(BaseModel):
    store_id: int
    tokens: list[TokenItem]
    user_message: Optional[str] = None
    auth_mode: Optional[str] = "api_key"


class ChatReplyRequest(BaseModel):
    answer: str


class ChatFollowUpRequest(BaseModel):
    message: str


class ChatAskRequest(BaseModel):
    message: str
    auth_mode: Optional[str] = "api_key"


SQL = """
    SELECT
        API_TOKEN_ID AS id,
        NAME         AS name,
        CREATED_AT
    FROM API_TOKEN
    WHERE STORE_ID = %s
      AND _FIVETRAN_DELETED = FALSE
    ORDER BY CREATED_AT DESC
"""


@app.post("/api/tokens")
def get_tokens(req: TokensRequest) -> dict:
    if _conn is None:
        raise HTTPException(status_code=503, detail="Snowflake not connected. Add credentials to backend/.env.")

    try:
        cur = _conn.cursor()
        cur.execute(SQL, (req.store_id,))
        columns = [col[0].lower() for col in cur.description]
        rows = cur.fetchall()
        tokens = [dict(zip(columns, row)) for row in rows]
        for t in tokens:
            if t.get("created_at"):
                t["created_at"] = str(t["created_at"])
        cur.close()
    except snowflake.connector.errors.DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"tokens": tokens}


@app.post("/api/splunk-search")
def splunk_search(req: SplunkSearchRequest) -> dict:
    splunk_url = splunk_client.build_store_total_url(req.store_id, req.time_value, req.time_unit)

    try:
        res = splunk_client.fetch_store_usage(req.store_id, req.time_value, req.time_unit)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

    if res.get("redirect"):
        return {"redirect": True, "splunk_url": splunk_url}

    return {
        "columns": res["columns"],
        "rows": res["rows"],
        "splunk_url": res["splunk_url"],
        "total": res["total"],
        "store_total_usage": res["store_total_usage"],
        "store_detail_usage": res["store_detail_usage"],
    }


@app.post("/api/analyze")
async def analyze_tokens(req: AnalyzeRequest) -> StreamingResponse:
    """Legacy endpoint — preserved for backward compatibility."""
    payload = {
        "store_id": req.store_id,
        "tokens": [t.model_dump() for t in req.tokens],
        "time_window_seconds": req.time_window_seconds,
    }

    async def stream():
        async for event in run_agent_loop(payload, conn=_conn):
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/start")
async def chat_start(req: ChatStartRequest) -> StreamingResponse:
    """Chatbot entry point — creates a session and starts the agentic loop."""
    auth_mode = req.auth_mode or "api_key"
    session = session_store.create_session(req.store_id)
    session["auth_mode"] = auth_mode

    payload = {
        "store_id": req.store_id,
        "tokens": [t.model_dump() for t in req.tokens],
        "user_message": req.user_message or "",
    }

    async def stream():
        async for event in run_agent_loop(payload, session=session, conn=_conn, auth_mode=auth_mode):
            session_store.touch_session(session["session_id"])
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/ask")
async def chat_ask(req: ChatAskRequest) -> StreamingResponse:
    """Free-form entry point — no store ID or pre-fetched tokens required."""
    auth_mode = req.auth_mode or "api_key"
    session = session_store.create_session(store_id=0)
    session["auth_mode"] = auth_mode
    payload = {
        "store_id": 0,
        "tokens": [],
        "user_message": req.message,
        "free_form": True,
    }

    async def stream():
        async for event in run_agent_loop(payload, session=session, conn=_conn, auth_mode=auth_mode):
            session_store.touch_session(session["session_id"])
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/{session_id}/reply")
async def chat_reply(session_id: str, req: ChatReplyRequest) -> dict:
    """Resume a paused session after HITL clarification."""
    s = session_store.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")
    if s["status"] != "waiting_for_clarification":
        raise HTTPException(status_code=409, detail=f"Session not paused (status: {s['status']}).")
    s["clarification_answer"] = req.answer
    s["clarification_event"].set()
    session_store.touch_session(session_id)
    return {"ok": True}


@app.post("/api/chat/{session_id}")
async def chat_followup(session_id: str, req: ChatFollowUpRequest) -> StreamingResponse:
    """Follow-up question after analysis is complete."""
    s = session_store.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")
    if s["status"] not in ("ready", "running"):
        raise HTTPException(status_code=409, detail=f"Session not ready (status: {s['status']}).")

    session_auth_mode = s.get("auth_mode", "api_key")

    async def stream():
        async for event in run_chat_turn(s, req.message, auth_mode=session_auth_mode):
            session_store.touch_session(session_id)
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
