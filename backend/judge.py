"""Independent judge — a separate LLM call with an adversarial prompt.

It did not produce the scores it reviews and it has a veto: `emit_recommendation`
is blocked until a token carries a passing verdict.

Fail-closed: if the judge cannot be reached, the verdict is NOT approved. An
unavailable auditor is not an approving auditor. The loop's rejection budget
(config.MAX_JUDGE_REJECTIONS) is what stops this from deadlocking — after two
failed verdicts the agent is told to emit `insufficient_data` instead.
"""

import json

import anthropic

from claude_cli import call_claude_cli
from config import CLEANUP_MIN_WINDOW_DAYS, JUDGE_MODEL, RATE_TIERS, SKILL_NAMES, SKILLS_DIR
from tools import JUDGE_VERDICT_TOOL
from util import load_text


def _unavailable(detail: str) -> dict:
    return {
        "approved": False,
        "objections": [f"Independent verification could not be completed: {detail}"],
        "reasoning": "Judge unavailable — treated as NOT approved (fail-closed).",
        "error": True,
    }


def _blocks(token_record: dict, score_data: dict, window_days: float) -> tuple:
    """Build (data_block, scores_block, skills_block) for the judge prompt."""
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
        f"Token id={token_record['id']} name=\"{token_record['name']}\" "
        f"age={token_record.get('age_days', -1)}d\n"
        f"calls={token_record.get('splunk_count')} calls/s={token_record.get('calls_per_second')} "
        f"rate_429={token_record.get('rate_429', 0)}\n"
        f"fill%: {fill_str}\n"
        f"shares_name_with={token_record.get('other_tokens_with_same_name', 0)} other token(s)\n"
        f"Observation window: {window_days} days (cleanup requires >= {CLEANUP_MIN_WINDOW_DAYS})\n"
    )
    scores_block = (
        f"rotation={scores_map['token_rotation']} — {score_data.get('rotation_reasoning', '')}\n"
        f"cleanup={scores_map['token_cleanup']} — {score_data.get('cleanup_reasoning', '')}\n"
        f"security_audit={scores_map['security_audit']} — {score_data.get('security_audit_reasoning', '')}\n"
        f"Implied recommended action (highest score): {best}"
    )
    skills_block = "\n\n".join(
        f"--- {name} ---\n{load_text(SKILLS_DIR / f'{name}.md')}" for name in SKILL_NAMES
    )
    return data_block, scores_block, skills_block


JUDGE_SYSTEM = (
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


async def run_judge(client, token_record: dict, score_data: dict, window_days: float) -> dict:
    """Audit one token's scores via the Anthropic API."""
    data_block, scores_block, skills_block = _blocks(token_record, score_data, window_days)
    judge_user = (
        f"TOKEN DATA:\n{data_block}\n\nPROPOSED SCORES:\n{scores_block}\n\n"
        f"SKILL CRITERIA:\n{skills_block}\n\nAudit these scores and record your verdict."
    )

    try:
        resp = await client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=1200,
            system=JUDGE_SYSTEM,
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
    except anthropic.APIError as e:
        return _unavailable(str(e)[:160])
    return _unavailable("no verdict returned")


async def run_judge_cli(token_record: dict, score_data: dict, window_days: float) -> dict:
    """Audit one token's scores via the `claude -p` CLI."""
    data_block, scores_block, skills_block = _blocks(token_record, score_data, window_days)
    judge_prompt = (
        f"{JUDGE_SYSTEM}\n\n"
        f"TOKEN DATA:\n{data_block}\n\nPROPOSED SCORES:\n{scores_block}\n\n"
        f"SKILL CRITERIA:\n{skills_block}\n\n"
        f"Respond with EXACTLY this JSON (no other text):\n"
        f'{{"approved": true, "objections": [], "reasoning": "..."}}'
    )
    try:
        response = await call_claude_cli(judge_prompt, JUDGE_MODEL)
        start = response.find("{")
        end = response.rfind("}") + 1
        if start < 0 or end <= start:
            return _unavailable("judge returned no JSON verdict")
        v = json.loads(response[start:end])
        return {
            "approved": bool(v.get("approved", False)),
            "objections": v.get("objections", []),
            "reasoning": v.get("reasoning", ""),
        }
    except Exception as e:
        return _unavailable(str(e)[:160])
