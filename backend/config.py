"""Constants and tunables shared by the agent modules.

Everything here is a knob, not logic. If you are changing behaviour, change a
skill file or a tool description first — this file should stay boring.
"""

from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"
SKILL_NAMES = ["token_rotation", "token_cleanup", "security_audit", "rate_limit_pressure"]

MODEL = "claude-sonnet-5"
JUDGE_MODEL = "claude-sonnet-5"

# Observation windows
DEFAULT_WINDOW_DAYS = 1
CLEANUP_MIN_WINDOW_DAYS = 30

# Loop budgets
MAX_TURNS = 60
MAX_JUDGE_REJECTIONS = 2   # after this many rejections for one token, stop re-scoring

# Snowflake TTL caches (seconds)
TOKEN_CACHE_TTL = 300
STORE_SETTINGS_CACHE_TTL = 300

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
