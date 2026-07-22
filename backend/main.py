from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
import snowflake.connector
from dotenv import load_dotenv
from typing import Optional
import os

from agent import run_agent_loop
import splunk_client

load_dotenv(override=True)

# Populated from ODS.CURATED.API_TOKEN via Snowflake MCP (store_id=20116)
DEMO_DATA: dict[int, list[dict]] = {
    20116: [
        {"id": 1152471, "name": "REVIEWS.io", "created_at": "2026-07-09T01:12:51"},
        {"id": 1152461, "name": "REVIEWS.io", "created_at": "2026-07-08T22:19:04"},
        {"id": 1152453, "name": "REVIEWS.io", "created_at": "2026-05-10T21:27:07"},
        {"id": 1152394, "name": "REVIEWS.io", "created_at": "2026-05-02T12:21:51"},
        {"id": 1151904, "name": "REVIEWS.io", "created_at": "2026-07-08T02:23:20"},
    ]
}

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
        print("Connecting to Snowflake...")
        _conn = create_connection()
        print("Snowflake connection established.")
    else:
        print("No Snowflake credentials — running in demo mode.")
    yield
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
    token_ids: list[int] = []  # empty = store-total query (all tokens for the store)
    time_value: int = 30
    time_unit: str = "days"


class TokenItem(BaseModel):
    id: int
    name: str
    created_at: str


class AnalyzeRequest(BaseModel):
    """The agent fetches usage itself — the client sends only the raw token list,
    the suggested window, and whether to run against synthetic demo usage."""
    store_id: int
    tokens: list[TokenItem]
    time_window_seconds: int = 604800
    demo: bool = False


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


def _demo_mode() -> bool:
    """True when neither Snowflake nor Watchtower is available — serve synthetic data."""
    return _conn is None and splunk_client.load_watchtower_token()[0] is None


@app.post("/api/tokens")
def get_tokens(req: TokensRequest) -> dict:
    if _conn is None:
        tokens = DEMO_DATA.get(req.store_id, [])
        return {"tokens": tokens, "demo": True}

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
    """Raw Splunk usage for the UI table. Store-total query (all tokens for the store)."""
    window_secs = splunk_client.window_seconds(req.time_value, req.time_unit)
    splunk_url = splunk_client.build_store_total_url(req.store_id, req.time_value, req.time_unit)

    # Demo mode — synthetic usage so the UI works offline.
    if _demo_mode():
        usage = splunk_client.demo_store_usage(req.store_id, window_secs)
        detail = usage["store_detail_usage"]
        columns = ["access_token_id", "method", "full_path", "status_code", "count"]
        rows = [[r[c] for c in columns] for r in detail]
        return {
            "columns": columns if detail else [],
            "rows": rows,
            "splunk_url": splunk_url,
            "total": len(detail),
            "store_total_usage": usage["store_total_usage"],
            "store_detail_usage": detail,
            "demo": True,
        }

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
    payload = {
        "store_id": req.store_id,
        "tokens": [t.model_dump() for t in req.tokens],
        "time_window_seconds": req.time_window_seconds,
        # Agent runs on synthetic usage when the client says demo OR when there is
        # no live Splunk access but we do have a demo profile for this store.
        "demo": req.demo or (_demo_mode() and splunk_client.has_demo_data(req.store_id)),
    }

    async def stream():
        async for event in run_agent_loop(payload):
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
