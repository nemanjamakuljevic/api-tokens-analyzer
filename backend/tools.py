"""Tool schemas offered to the model.

Descriptions say *what a tool does* and *when it is the right call*. They do not
carry scoring thresholds or skill-selection criteria — those live in
`skills/decision_tree.md` and the individual skill files, so there is exactly one
place to change them.
"""

from config import SKILL_NAMES

FETCH_TOKEN_USAGE_TOOL = {
    "name": "fetch_token_usage",
    "description": (
        "Pull Splunk API usage for this store over an observation window you choose. "
        "Returns per-token call counts, avg calls/s, rate-limit fill %, HTTP 429 counts, "
        "and endpoint breakdown. Choose the window based on what you are investigating, "
        "and re-query with a longer window when the result is inconclusive."
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
        "Load ONE scoring framework's criteria. The decision tree in your system context "
        "maps usage signals to skills — follow it, and load only the skills the data "
        "warrants. Score a token only against criteria from skills you have loaded."
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
        "If rejected, re-score to address the objections, then verify again — but stop "
        "re-scoring the same token once the auditor has rejected it twice."
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
        "definitive conclusion, or when verification could not be completed — include a "
        "note explaining what is missing."
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
        "The decision tree lists the blockers that genuinely require this. "
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
        "This tool only writes down the target — it fetches nothing. You decide what to "
        "call next. If store_id is absent and no token IDs were mentioned, add "
        "'no store_id given' to open_questions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request_type": {
                "type": "string",
                "enum": [
                    "full_audit", "single_token_diagnosis", "rate_limit_investigation",
                    "cleanup_request", "security_concern", "error_diagnosis",
                    "token_limit_exhaustion", "general_question",
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
        "Fetch the token roster (id, name, created_at) for a store from Snowflake, plus the "
        "store's rate-limit and token-quota settings. "
        "Use ONLY for full audits or when you need to discover what tokens exist. "
        "NEVER call this if the user already named specific token IDs — resolve those with "
        "lookup_token_store instead. Calling this unnecessarily fetches all tokens when "
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
        "Given a token ID, look up which store it belongs to in Snowflake, along with that "
        "store's rate-limit settings. "
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

FETCH_ERROR_PATTERNS_TOOL = {
    "name": "fetch_error_patterns",
    "description": (
        "Query Splunk for non-success HTTP errors (4xx/5xx, excluding 429) per token. "
        "Use when the user asks why a token is hitting errors, or when fetch_token_usage "
        "shows elevated non-429 error counts. Returns a per-token, per-endpoint error "
        "breakdown. Pair with load_recharge_status_codes to interpret the codes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "description": "Window to search for errors.",
            },
            "token_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Specific token IDs to analyze. Empty = all store tokens.",
            },
            "status_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Status codes to include. Defaults to [400,401,403,404,422,500,503].",
            },
        },
        "required": ["window_days"],
    },
}

FETCH_TOKEN_ACTIVITY_TOOL = {
    "name": "fetch_token_activity",
    "description": (
        "Fetch raw (non-aggregated) API request rows from Splunk with a specific time window. "
        "Use to answer 'which token made this call?' or 'what was happening between 14:30 and 14:45?' "
        "Unlike fetch_token_usage (which aggregates counts), this returns individual rows with "
        "timestamps so you can pinpoint specific calls. "
        "Narrow the time range and add filters to keep results manageable — avoid querying "
        "large windows without at least one filter (token_id, method, path_contains, or status_code)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "time_start": {
                "type": "string",
                "description": (
                    "Start of window. Relative (e.g. '-2h', '-30m', '-1d') or Splunk absolute "
                    "format MM/DD/YYYY:HH:MM:SS. For 'which token at 14:35?', prefer '-90m' "
                    "and narrow with path_contains or status_code."
                ),
            },
            "time_end": {
                "type": "string",
                "description": "End of window. Relative or absolute. Defaults to 'now'.",
            },
            "token_id": {
                "type": ["integer", "null"],
                "description": "Optional: filter to a specific token ID.",
            },
            "method": {
                "type": ["string", "null"],
                "description": "Optional: filter to a specific HTTP method (GET, POST, PUT, DELETE).",
            },
            "path_contains": {
                "type": ["string", "null"],
                "description": "Optional: filter to paths containing this substring (e.g. 'subscriptions').",
            },
            "status_code": {
                "type": ["string", "null"],
                "description": "Optional: filter to a specific HTTP status code.",
            },
            "max_rows": {
                "type": "integer",
                "description": "Maximum rows to return. Defaults to 100.",
            },
        },
        "required": ["time_start"],
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
    FETCH_ERROR_PATTERNS_TOOL,
    FETCH_TOKEN_ACTIVITY_TOOL,
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
