"""The agentic loop.

One `while`. Each turn the model sees everything that happened so far and picks
the next tool — or stops. Python never sequences the run:

  * which tools fire, in what order, and how many times
  * how long an observation window is, and whether to re-query a longer one
  * which scoring skills get loaded
  * when the analysis is finished

...are all decisions the model makes from the tool results it gets back.

The only things Python enforces are guardrails, not steps:
  - `emit_recommendation` is blocked until an independent judge approves the token
  - the judge fails closed — an unreachable auditor is not an approving auditor
  - a token may only be re-scored MAX_JUDGE_REJECTIONS times before the agent must
    fall back to `insufficient_data` or ask a human
  - MAX_TURNS caps the run

Everything else — tool schemas, tool execution, enrichment, the judge — lives in
its own module. This file is the loop and nothing else.
"""

import asyncio
import os
from typing import AsyncGenerator, Optional

import anthropic

from claude_cli import build_cli_prompt, call_claude_cli, parse_cli_response
from config import MAX_TURNS, MODEL, SKILLS_DIR
from dispatch import dispatch_tool
from tools import TOOLS
from util import clean_str, load_text, sse, ts


def _system_prompt() -> str:
    """Role and ground rules, plus the single source of routing truth."""
    return (
        load_text(SKILLS_DIR / "system_prompt.md")
        + "\n\n"
        + load_text(SKILLS_DIR / "decision_tree.md")
    )


# ── One turn, API transport ───────────────────────────────────────────────────────

async def _stream_turn(
    client,
    messages: list,
    system_prompt: str,
    tools: list,
    ctx: dict,
    tool_choice: dict = None,
) -> AsyncGenerator[str, None]:
    """Stream one LLM turn. Populates ctx['_turn_*']. Yields SSE strings."""
    ctx["_turn_assistant_content"] = []
    ctx["_turn_tool_results"] = []
    ctx["_turn_has_tool_use"] = False
    ctx["_clarification_pending"] = None
    ctx["_turn_error"] = None

    narration_parts: list = []

    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            system=system_prompt,
            tools=tools,
            tool_choice=tool_choice or {"type": "auto"},
            thinking={"type": "adaptive", "display": "summarized"},
            messages=messages,
        ) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)

                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", None) == "tool_use":
                        ctx["_turn_has_tool_use"] = True

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None) if delta else None

                    if dtype == "thinking_delta":
                        chunk = getattr(delta, "thinking", "") or ""
                        if chunk:
                            yield sse({"type": "thought_chunk", "delta": chunk, "ts": ts()})

                    elif dtype == "text_delta":
                        chunk = getattr(delta, "text", "") or ""
                        if chunk:
                            if ctx["_turn_has_tool_use"]:
                                narration_parts.append(chunk)
                            else:
                                yield sse({"type": "content_chunk", "delta": chunk, "ts": ts()})

            # Complete message, including thinking signatures we must echo back
            final = await stream.get_final_message()

        for block in final.content:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                ctx["_turn_assistant_content"].append({
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                })
            elif btype == "text":
                ctx["_turn_assistant_content"].append({
                    "type": "text",
                    "text": getattr(block, "text", ""),
                })
            elif btype == "tool_use":
                ctx["_turn_assistant_content"].append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        narration = "".join(narration_parts).strip()[:500]
        first_tool = True
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            if block.name == "clarify_with_user":
                ctx["_clarification_pending"] = {
                    "tool_use_id": block.id,
                    "question": block.input.get("question", ""),
                    "context": block.input.get("context", ""),
                }
                yield sse({
                    "type": "clarification_request",
                    "question": block.input.get("question", ""),
                    "context": block.input.get("context", ""),
                    "ts": ts(),
                })
            else:
                result_text, sse_event = await dispatch_tool(block.name, block.input, ctx)
                if first_tool and narration:
                    sse_event["narration"] = narration
                    first_tool = False
                yield sse({**sse_event, "ts": ts()})
                for extra_event in ctx.pop("_pending_sse_events", []):
                    yield sse({**extra_event, "ts": ts()})
                ctx["_turn_tool_results"].append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

    except anthropic.APIError as e:
        yield sse({"type": "error", "message": f"API error: {e}"})
        ctx["_turn_error"] = str(e)


# ── One turn, CLI transport ───────────────────────────────────────────────────────

async def _stream_turn_cli(
    messages: list,
    system_prompt: str,
    tools: list,
    ctx: dict,
    tool_choice: dict = None,
) -> AsyncGenerator[str, None]:
    """One agent turn using `claude -p` (no streaming — the response arrives at once)."""
    ctx["_turn_assistant_content"] = []
    ctx["_turn_tool_results"] = []
    ctx["_turn_has_tool_use"] = False
    ctx["_clarification_pending"] = None
    ctx["_turn_error"] = None

    known_tools = {t["name"] for t in tools}
    force_tool = tool_choice.get("name") if tool_choice and tool_choice.get("type") == "tool" else None
    prompt = build_cli_prompt(system_prompt, messages, tools, force_tool=force_tool)

    try:
        response = await call_claude_cli(prompt)
    except RuntimeError as e:
        yield sse({"type": "error", "message": str(e)})
        ctx["_turn_error"] = str(e)
        return

    text, tool_call = parse_cli_response(response, known_tools)

    if tool_call:
        ctx["_turn_has_tool_use"] = True
        name = tool_call["name"]
        inp = tool_call["input"]
        ctx["_turn_assistant_content"].append(tool_call)

        if name == "clarify_with_user":
            ctx["_clarification_pending"] = {
                "tool_use_id": tool_call["id"],
                "question": inp.get("question", ""),
                "context": inp.get("context", ""),
            }
            yield sse({
                "type": "clarification_request",
                "question": inp.get("question", ""),
                "context": inp.get("context", ""),
                "ts": ts(),
            })
        else:
            result_text, sse_event = await dispatch_tool(name, inp, ctx)
            if text:
                sse_event["narration"] = text[:500]
            yield sse({**sse_event, "ts": ts()})
            for extra_event in ctx.pop("_pending_sse_events", []):
                yield sse({**extra_event, "ts": ts()})
            ctx["_turn_tool_results"].append({
                "type": "tool_result",
                "tool_use_id": tool_call["id"],
                "content": result_text,
            })
    elif text:
        ctx["_turn_assistant_content"].append({"type": "text", "text": text})
        yield sse({"type": "content_chunk", "delta": text, "ts": ts()})


# ── Result assembly ───────────────────────────────────────────────────────────────

def _score_row(ctx: dict, token_id: int, token_name: str) -> dict:
    record = ctx["token_records"].get(token_id, {})
    return {
        "token_id": token_id,
        "token_name": token_name,
        "splunk_count": record.get("splunk_count"),
        "calls_per_second": record.get("calls_per_second"),
        "rate_429": record.get("rate_429"),
        "window_days": round(ctx["window_seconds"] / 86400, 2) if ctx["window_seconds"] else None,
    }


def _score_fields(score: dict) -> dict:
    return {
        "rotation_score": score.get("rotation_score", 0),
        "rotation_reasoning": clean_str(score.get("rotation_reasoning", "")),
        "cleanup_score": score.get("cleanup_score", 0),
        "cleanup_reasoning": clean_str(score.get("cleanup_reasoning", "")),
        "security_audit_score": score.get("security_audit_score", 0),
        "security_audit_reasoning": clean_str(score.get("security_audit_reasoning", "")),
    }


def _build_token_scores(ctx: dict) -> list:
    """Per-token cards for the UI: committed recommendations first, partial work second."""
    analyzed = set(ctx.get("scored", {})) | set(ctx.get("recommendations", {}))

    if not ctx.get("tokens"):
        # No roster at all — report only what was actually committed.
        rows = []
        for token_id, rec in ctx.get("recommendations", {}).items():
            score = ctx["scored"].get(token_id, {})
            name = rec.get("token_name", score.get("token_name", f"id={token_id}"))
            rows.append({
                **_score_row(ctx, token_id, name),
                **_score_fields(score),
                "recommended_action": rec.get("recommended_action", "no_action"),
                "recommendation": rec.get("recommendation", ""),
                "verification_approved": rec.get("verification_approved", False),
                "verification_reasoning": rec.get("verification_reasoning", ""),
            })
        return rows

    rows = []
    for t in ctx["tokens"]:
        tid = t["id"]
        rec = ctx["recommendations"].get(tid)
        score = ctx["scored"].get(tid, {})
        ver = ctx["verified"].get(tid, {})
        base = _score_row(ctx, tid, t["name"])

        if rec:
            base.update({
                **_score_fields(score),
                "recommended_action": rec.get("recommended_action", "no_action"),
                "recommendation": rec.get("recommendation", ""),
                "verification_approved": rec.get("verification_approved", False),
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
                **_score_fields(score),
                "recommended_action": best if max(scores_map.values()) >= 20 else "no_action",
                "recommendation": "Analysis incomplete — not emitted.",
                "verification_approved": ver.get("approved", False),
                "verification_reasoning": ver.get("reasoning", ""),
            })
        else:
            # Skip untouched tokens when at least one other token was analyzed.
            if analyzed:
                continue
            base.update({
                **_score_fields({}),
                "recommended_action": "no_action",
                "recommendation": "Token was not analyzed in this session.",
                "verification_approved": False,
                "verification_reasoning": "",
            })
        rows.append(base)
    return rows


def _new_ctx(client, auth_mode: str, store_id: int, tokens: list, conn) -> dict:
    return {
        "client": client,
        "auth_mode": auth_mode,
        "store_id": store_id,
        "tokens": list(tokens),
        "total_tokens": len(tokens),
        "window_seconds": 0,
        "max_window_days": 0,
        "token_records": {},
        "orphaned": [],
        "skills_loaded": set(),
        "scored": {},
        "verified": {},
        "reject_counts": {},
        "recommendations": {},
        "fetch_count": 0,
        "prev_scored": set(),
        "intent": None,
        "conn": conn,
        "store_settings": {},
    }


# ── Main agentic loop ─────────────────────────────────────────────────────────────

async def run_agent_loop(
    data: dict,
    session: Optional[dict] = None,
    conn=None,
    auth_mode: str = "api_key",
) -> AsyncGenerator[str, None]:
    from memory_store import build_memory_context, save_run

    if auth_mode == "claude_cli":
        client = None
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            yield sse({"type": "error", "message": "ANTHROPIC_API_KEY not set. Add it to backend/.env."})
            return
        client = anthropic.AsyncAnthropic(api_key=api_key)

    system_prompt = _system_prompt()

    tokens = data.get("tokens") or []
    store_id = data.get("store_id") or 0
    user_message = data.get("user_message", "").strip()
    free_form = bool(data.get("free_form"))
    session_id = session["session_id"] if session else None

    if session:
        yield sse({"type": "session_started", "session_id": session["session_id"], "ts": ts()})

    yield sse({"type": "step", "label": "System prompt loaded",
               "detail": "Agent role, rate-limit model and decision tree initialized.", "ts": ts()})

    if free_form:
        yield sse({"type": "step", "label": "Free-form request received",
                   "detail": "Agent will determine scope and fetch what it needs.", "ts": ts()})
    else:
        yield sse({"type": "step", "label": "Tokens received (no usage yet)",
                   "detail": f"{len(tokens)} token(s) for store {store_id}. "
                             f"Agent will choose the observation window.", "ts": ts()})

    # Episodic memory from prior runs (skip for free-form with no store yet)
    memory_context = build_memory_context(store_id) if store_id else ""
    memory_block = f"{memory_context}\n\n" if memory_context else ""

    if free_form:
        initial_prompt = (
            f"{memory_block}"
            f"{user_message}\n\n"
            f"Call record_intent first to confirm your understanding, then investigate."
        )
    else:
        token_lines = "\n".join(
            f"  id={t['id']}, name=\"{t['name']}\", created_at={t['created_at']}" for t in tokens
        )
        goal_line = f'User goal: "{user_message}"\n\n' if user_message else ""
        initial_prompt = (
            f"{memory_block}"
            f"{goal_line}"
            f"Store {store_id} — {len(tokens)} API token(s). "
            f"Your objective: ensure every token has a correct, judge-approved recommendation.\n\n"
            f"Tokens:\n{token_lines}\n"
        )

    ctx = _new_ctx(client, auth_mode, store_id, tokens, conn)
    if session:
        session["ctx"] = ctx

    messages: list = [{"role": "user", "content": initial_prompt}]
    turn_count = 0

    while turn_count < MAX_TURNS:
        turn_count += 1

        # Turn 1 of a free-form request writes down the target before anything else,
        # so the run has a stated scope to be judged against. Every later turn is
        # the model's own choice.
        turn_tool_choice = None
        if free_form and turn_count == 1:
            turn_tool_choice = {"type": "tool", "name": "record_intent"}

        if auth_mode == "claude_cli":
            async for event_str in _stream_turn_cli(messages, system_prompt, TOOLS, ctx,
                                                    tool_choice=turn_tool_choice):
                yield event_str
        else:
            async for event_str in _stream_turn(client, messages, system_prompt, TOOLS, ctx,
                                                tool_choice=turn_tool_choice):
                yield event_str

        if ctx.get("_turn_error"):
            return

        # Human-in-the-loop pause
        clarification_pending = ctx.get("_clarification_pending")
        if clarification_pending:
            if session:
                session["status"] = "waiting_for_clarification"
                session["clarification_event"].clear()

                elapsed = 0
                timeout = 300
                while not session["clarification_event"].is_set() and elapsed < timeout:
                    await asyncio.sleep(25)
                    elapsed += 25
                    if not session["clarification_event"].is_set():
                        yield sse({"type": "ping", "ts": ts()})

                if not session["clarification_event"].is_set():
                    yield sse({"type": "error", "message": "Clarification timed out after 5 minutes."})
                    return

                answer = session.pop("clarification_answer", "No answer provided.")
                session["clarification_event"].clear()
                session["status"] = "running"
            else:
                answer = "No session available to receive user reply — proceeding with best judgment."

            ctx["_turn_tool_results"].append({
                "type": "tool_result",
                "tool_use_id": clarification_pending["tool_use_id"],
                "content": f"The user replied: {answer}",
            })

        messages.append({"role": "assistant", "content": ctx["_turn_assistant_content"]})
        if ctx["_turn_tool_results"]:
            messages.append({"role": "user", "content": ctx["_turn_tool_results"]})

        # The agent decides it is done by writing prose instead of calling a tool.
        if not ctx["_turn_has_tool_use"]:
            final_text = ""
            for block in ctx["_turn_assistant_content"]:
                if block.get("type") == "text":
                    final_text = clean_str(block.get("text", ""))
                    break

            token_scores = _build_token_scores(ctx)
            all_approved = all(v.get("approved", False) for v in ctx["verified"].values())
            combined_reasoning = "; ".join(
                v.get("reasoning", "") for v in ctx["verified"].values() if v.get("reasoning")
            )[:500]

            final_store_id = ctx.get("store_id") or store_id
            if final_store_id:
                save_run(
                    store_id=final_store_id,
                    session_id=session_id or "no-session",
                    store_summary=final_text or "Analysis complete.",
                    token_outcomes=[
                        {"token_id": r["token_id"], "action": r["recommended_action"]}
                        for r in token_scores
                    ],
                    window_days=ctx["max_window_days"],
                )

            yield sse({
                "type": "done",
                "session_id": session_id,
                "token_scores": token_scores,
                "store_summary": final_text or "Analysis complete.",
                "iterations": turn_count,
                "approved": all_approved,
                "verification_reasoning": combined_reasoning,
            })
            if session:
                session["status"] = "ready"
                session["messages"] = messages
            return

    yield sse({
        "type": "done",
        "session_id": session_id,
        "token_scores": _build_token_scores(ctx),
        "store_summary": f"Analysis completed after {turn_count} turns (safety limit reached).",
        "iterations": turn_count,
        "approved": False,
        "verification_reasoning": "Max turns limit reached.",
    })
    if session:
        session["status"] = "ready"
        session["messages"] = messages


# ── Follow-up chat turn ───────────────────────────────────────────────────────────

async def run_chat_turn(
    session: dict,
    user_message: str,
    auth_mode: str = "api_key",
) -> AsyncGenerator[str, None]:
    """Single follow-up turn on an already-completed session."""
    from session_store import update_dialogue

    chat_system = (
        _system_prompt()
        + "\n\nYou are now in follow-up chat mode. Answer the user's question based on the "
        "analysis you just completed. Be concise and cite specific fill percentages and token "
        "IDs from your earlier analysis."
    )

    update_dialogue(session, "human", user_message)
    session["messages"].append({"role": "user", "content": user_message})
    session["status"] = "running"

    if auth_mode == "claude_cli":
        prompt = build_cli_prompt(chat_system, session["messages"], [])
        try:
            reply_text = (await call_claude_cli(prompt)).strip()
        except RuntimeError as e:
            session["status"] = "error"
            yield sse({"type": "error", "message": str(e)})
            return
        yield sse({"type": "content_chunk", "delta": reply_text, "ts": ts()})
        session["messages"].append({"role": "assistant", "content": [{"type": "text", "text": reply_text}]})
        update_dialogue(session, "assistant", reply_text)
        session["status"] = "ready"
        yield sse({"type": "chat_done", "ts": ts()})
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        yield sse({"type": "error", "message": "ANTHROPIC_API_KEY not set."})
        return

    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=chat_system,
            messages=session["messages"],
        ) as stream:
            async for event in stream:
                if getattr(event, "type", None) == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        chunk = getattr(delta, "text", "") or ""
                        if chunk:
                            yield sse({"type": "content_chunk", "delta": chunk, "ts": ts()})

            final = await stream.get_final_message()

        reply_text = ""
        for block in final.content:
            if getattr(block, "type", None) == "text":
                reply_text = getattr(block, "text", "")
                break

        session["messages"].append({"role": "assistant", "content": [{"type": "text", "text": reply_text}]})
        update_dialogue(session, "assistant", reply_text)
        session["status"] = "ready"
        yield sse({"type": "chat_done", "ts": ts()})

    except anthropic.APIError as e:
        session["status"] = "error"
        yield sse({"type": "error", "message": f"API error: {e}"})
