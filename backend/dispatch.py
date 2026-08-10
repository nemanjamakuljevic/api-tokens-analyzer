"""Tool execution. One function, one branch per tool.

Each branch returns `(result_text, sse_event)`:
  result_text — what the model sees as the tool result. This is the only channel
                that can change the agent's next decision, so it carries the
                nudges: "window too short", "judge rejected, here is why",
                "rejection budget spent, emit insufficient_data instead".
  sse_event   — what the UI shows in the Thinking Process panel.

No branch decides what happens next. It reports state and hands control back.
"""

import asyncio

import splunk_client
import usage as usage_mod
from config import (
    CLEANUP_MIN_WINDOW_DAYS,
    DEFAULT_WINDOW_DAYS,
    MAX_JUDGE_REJECTIONS,
    RECHARGE_STATUS_CODES,
    SKILL_NAMES,
    SKILLS_DIR,
)
from judge import run_judge, run_judge_cli
from util import clean_str, load_text


async def dispatch_tool(name: str, inp: dict, ctx: dict):
    """Execute one tool call. Returns (result_text, sse_event)."""

    # ── Evidence ────────────────────────────────────────────────────────────────

    if name == "fetch_token_usage":
        window_days = int(inp.get("window_days", DEFAULT_WINDOW_DAYS))
        window_secs = window_days * 86400
        ctx["fetch_count"] = ctx.get("fetch_count", 0) + 1
        is_re_query = ctx["fetch_count"] > 1
        prev_window_days = ctx["max_window_days"] if is_re_query else None
        # Filter to known token IDs so the query is scoped; fall back to store-wide.
        known_token_ids = [t["id"] for t in ctx.get("tokens", []) if t.get("id")]
        try:
            res = await asyncio.to_thread(
                splunk_client.fetch_store_usage, ctx["store_id"], window_days, "days",
                token_ids=known_token_ids or None,
            )
            if res.get("redirect"):
                return (
                    "Splunk authentication required — cannot fetch live usage. "
                    "Authenticate via the Splunk tab and retry.",
                    {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required",
                     "detail": res.get("splunk_url", "")},
                )
            usage = {"store_detail_usage": res["store_detail_usage"]}
        except Exception as splunk_err:
            return (
                f"Splunk fetch failed: {splunk_err}. Cannot proceed without usage data.",
                {"type": "step", "tool": name, "label": "Fetch error", "detail": str(splunk_err)},
            )

        enriched = usage_mod.enrich(ctx["store_id"], ctx["tokens"], usage["store_detail_usage"], window_secs)
        ctx["window_seconds"] = window_secs
        ctx["max_window_days"] = max(ctx["max_window_days"], window_days)
        for rec in enriched["tokens"]:
            ctx["token_records"][rec["id"]] = rec
        ctx["orphaned"] = enriched.get("orphaned_tokens", [])
        # Without a roster, treat Splunk-only tokens as real records so
        # score/verify/emit can find them by token_id.
        if not ctx["tokens"]:
            for orec in ctx["orphaned"]:
                tid = int(orec["id"])
                if tid not in ctx["token_records"]:
                    ctx["token_records"][tid] = usage_mod.orphan_record(orec)

        summary = usage_mod.usage_summary(enriched, ctx.get("store_settings"))
        splunk_rows = [
            {
                "id": t["id"],
                "name": t["name"],
                "calls": t["splunk_count"],
                "cps": t["calls_per_second"],
                "rate_429": t["rate_429"],
                "fill_top": round(max(t["fill_pct"].values()), 1) if t.get("fill_pct") else 0,
            }
            for t in enriched["tokens"]
        ]
        return (
            f"Fetched usage over a {window_days}-day window.\n\n{summary}",
            {"type": "step", "tool": name,
             "call": f"fetch_token_usage(window_days={window_days})",
             "label": f"Fetched usage: {window_days}-day window",
             "detail": inp.get("reason", f"{enriched['total']} tokens, {len(ctx['orphaned'])} orphaned"),
             "splunk_rows": splunk_rows,
             "re_query": is_re_query,
             "prev_window_days": prev_window_days},
        )

    if name == "fetch_429_errors":
        window_days = int(inp.get("window_days", DEFAULT_WINDOW_DAYS))
        token_ids_filter = {str(i) for i in inp.get("token_ids", [])}

        splunk_token_ids = list(inp.get("token_ids", [])) or [t["id"] for t in ctx.get("tokens", []) if t.get("id")]
        try:
            res = await asyncio.to_thread(
                splunk_client.fetch_store_usage, ctx["store_id"], window_days, "days",
                token_ids=splunk_token_ids or None,
            )
            if res.get("redirect"):
                return (
                    "Splunk authentication required.",
                    {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required", "detail": ""},
                )
            raw = res["store_detail_usage"]
        except Exception as splunk_err:
            return (
                f"Splunk fetch failed: {splunk_err}. Cannot retrieve 429 data.",
                {"type": "step", "tool": name, "label": "Fetch error", "detail": str(splunk_err)},
            )

        rows_429 = [r for r in raw if str(r.get("status_code", "")) == "429"]
        if token_ids_filter:
            rows_429 = [r for r in rows_429 if str(r.get("access_token_id", "")) in token_ids_filter]

        by_token: dict = {}
        for row in rows_429:
            tid = str(row.get("access_token_id", "unknown"))
            cnt = int(row.get("count", 0))
            by_token[tid] = by_token.get(tid, 0) + cnt

        token_name_map = {str(t["id"]): t["name"] for t in ctx["tokens"]}
        total_429 = sum(by_token.values())

        summary_lines = [f"429 analysis — {window_days}-day window: {total_429} total rate-limit hits"]
        splunk_429_rows = []
        for tid, cnt in sorted(by_token.items(), key=lambda x: -x[1]):
            tname = token_name_map.get(tid, f"id={tid}")
            summary_lines.append(f"  token {tid} ({tname}): {cnt} 429s")
            splunk_429_rows.append({"token_id": tid, "token_name": tname, "count_429": cnt})

        if not total_429:
            summary_lines.append("  No 429 errors found in this window.")

        ep = usage_mod.build_endpoint_summary(rows_429)
        if ep:
            summary_lines.append(f"Top 429 endpoints: {ep}")

        return (
            "\n".join(summary_lines),
            {
                "type": "step",
                "tool": name,
                "call": f"fetch_429_errors(window_days={window_days})",
                "label": f"429 analysis: {window_days}-day window",
                "detail": f"{total_429} rate-limit hits across {len(by_token)} token(s)",
                "splunk_429_rows": splunk_429_rows,
            },
        )

    if name == "fetch_error_patterns":
        window_days = int(inp.get("window_days", DEFAULT_WINDOW_DAYS))
        token_ids_filter = inp.get("token_ids", [])
        status_codes = inp.get("status_codes") or None

        splunk_token_ids = token_ids_filter or [t["id"] for t in ctx.get("tokens", []) if t.get("id")]
        try:
            res = await asyncio.to_thread(
                splunk_client.fetch_error_patterns, ctx["store_id"], window_days, "days",
                token_ids=splunk_token_ids or None, status_codes=status_codes,
            )
            if res.get("redirect"):
                return (
                    "Splunk authentication required — cannot fetch error patterns.",
                    {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required", "detail": ""},
                )
            rows = res["rows"]
        except Exception as splunk_err:
            return (
                f"Splunk fetch failed: {splunk_err}.",
                {"type": "step", "tool": name, "label": "Fetch error", "detail": str(splunk_err)},
            )

        by_token: dict = {}
        for row in rows:
            by_token.setdefault(str(row["token_id"]), []).append(row)

        token_name_map = {str(t["id"]): t["name"] for t in ctx.get("tokens", [])}
        total_errors = sum(r["count"] for r in rows)

        summary_lines = [
            f"Error pattern analysis — {window_days}-day window: {total_errors} total errors (excl. 429). "
            f"Cross-reference with load_recharge_status_codes for code meanings."
        ]
        error_rows_ui = []
        for tid, tok_rows in sorted(by_token.items(), key=lambda x: -sum(r["count"] for r in x[1])):
            tname = token_name_map.get(tid, f"id={tid}")
            tok_total = sum(r["count"] for r in tok_rows)
            summary_lines.append(f"  Token {tid} ({tname}): {tok_total} errors")
            for row in sorted(tok_rows, key=lambda x: -x["count"])[:8]:
                summary_lines.append(
                    f"    {row['count']}× {row['method']} {row['path']} [{row['status_code']}]"
                )
            error_rows_ui.append({
                "token_id": tid,
                "token_name": tname,
                "total": tok_total,
                "breakdown": [
                    {"method": r["method"], "path": r["path"],
                     "status_code": r["status_code"], "count": r["count"]}
                    for r in sorted(tok_rows, key=lambda x: -x["count"])[:10]
                ],
            })

        if not total_errors:
            summary_lines.append("  No non-429 errors found in this window.")

        return (
            "\n".join(summary_lines),
            {
                "type": "step",
                "tool": name,
                "call": f"fetch_error_patterns(window_days={window_days})",
                "label": f"Error patterns: {window_days}-day window",
                "detail": f"{total_errors} errors across {len(by_token)} token(s)",
                "error_rows": error_rows_ui,
            },
        )

    if name == "fetch_token_activity":
        time_start = inp.get("time_start", "-1h")
        time_end = inp.get("time_end", "now")
        try:
            res = await asyncio.to_thread(
                splunk_client.fetch_token_activity, ctx["store_id"],
                time_start, time_end,
                inp.get("token_id") or None,
                inp.get("method") or None,
                inp.get("path_contains") or None,
                inp.get("status_code") or None,
                int(inp.get("max_rows", 100)),
            )
            if res.get("redirect"):
                return (
                    "Splunk authentication required — cannot fetch activity.",
                    {"type": "step", "tool": name, "label": "Fetch blocked — Splunk auth required", "detail": ""},
                )
            rows = res["rows"]
        except Exception as splunk_err:
            return (
                f"Splunk fetch failed: {splunk_err}.",
                {"type": "step", "tool": name, "label": "Fetch error", "detail": str(splunk_err)},
            )

        token_name_map = {str(t["id"]): t["name"] for t in ctx.get("tokens", [])}

        if not rows:
            summary = (
                f"No requests found between {time_start} and {time_end} with the given filters. "
                f"Try widening the time window or removing filters."
            )
        else:
            token_ids_seen = {r["token_id"] for r in rows}
            summary_lines = [
                f"Activity ({time_start} → {time_end}): {len(rows)} request(s) "
                f"from {len(token_ids_seen)} distinct token(s)"
            ]
            for row in rows[:30]:
                tname = token_name_map.get(str(row["token_id"]), f"id={row['token_id']}")
                summary_lines.append(
                    f"  {row['timestamp']}  token={row['token_id']} ({tname})  "
                    f"{row['method']} {row['path']}  [{row['status_code']}]  {row.get('duration_ms', '')}ms"
                )
            if len(rows) > 30:
                summary_lines.append(f"  … {len(rows) - 30} more rows (increase max_rows or narrow filters)")
            summary = "\n".join(summary_lines)

        return (
            summary,
            {
                "type": "step",
                "tool": name,
                "call": f"fetch_token_activity({time_start}→{time_end})",
                "label": f"Activity lookup: {time_start} → {time_end}",
                "detail": f"{len(rows)} request(s)",
                "activity_rows": rows[:50],
            },
        )

    # ── Reference material ──────────────────────────────────────────────────────

    if name == "load_recharge_status_codes":
        codes_text = "\n".join(
            f"  {c['code']} {c['name']}: {c['description']}" for c in RECHARGE_STATUS_CODES
        )
        return (
            f"ReCharge 2021-11 API status codes:\n{codes_text}",
            {
                "type": "status_codes",
                "codes": RECHARGE_STATUS_CODES,
                "source": "https://developer.rechargepayments.com/2021-11/responses",
            },
        )

    if name == "load_rate_limit_docs":
        return load_text(SKILLS_DIR / "rate_limit_docs.md"), {"type": "rate_limit_docs"}

    if name == "load_skill":
        skill = inp.get("skill_name", "")
        if skill not in SKILL_NAMES:
            return (
                f"Unknown skill '{skill}'. Choose one of: {', '.join(SKILL_NAMES)}.",
                {"type": "step", "tool": name, "label": f"Unknown skill: {skill}", "detail": ""},
            )
        ctx["skills_loaded"].add(skill)
        criteria = load_text(SKILLS_DIR / f"{skill}.md")
        return (
            f"Loaded skill '{skill}'. Apply these criteria:\n\n{criteria}",
            {"type": "step", "tool": name, "call": f"load_skill('{skill}')",
             "label": f"Loaded skill: {skill}", "detail": "scoring framework loaded"},
        )

    # ── Score → verify → emit ───────────────────────────────────────────────────

    if name == "score_single_token":
        if not ctx["token_records"]:
            return (
                "No usage data yet. Call fetch_token_usage before scoring.",
                {"type": "step", "tool": name, "label": "Scoring blocked — no usage data", "detail": ""},
            )
        token_id = inp.get("token_id")
        record = ctx["token_records"].get(token_id, {})
        window_days_float = ctx["window_seconds"] / 86400 if ctx["window_seconds"] else 0
        splunk_count = record.get("splunk_count") or 0
        rejects = ctx["reject_counts"].get(token_id, 0)

        notes = ""
        if not ctx["skills_loaded"]:
            notes += (
                "\n⚠ No skill loaded yet — scores recorded, but the judge may reject them for "
                "lacking criteria backing. Consider loading a relevant skill and re-scoring."
            )
        if splunk_count == 0 and 0 < window_days_float < CLEANUP_MIN_WINDOW_DAYS:
            notes += (
                f"\n⚠ Token {token_id} shows 0 calls over only {window_days_float:.0f} days — "
                f"consider re-fetching with a >={CLEANUP_MIN_WINDOW_DAYS}-day window to confirm idleness, "
                f"or emit recommended_action='insufficient_data'."
            )
        if rejects >= MAX_JUDGE_REJECTIONS:
            notes += (
                f"\n⚠ The auditor has already rejected token {token_id} {rejects} times. Do not keep "
                f"re-scoring it — emit_recommendation(recommended_action='insufficient_data') instead, "
                f"or ask the user."
            )

        is_rescore = token_id in ctx.get("prev_scored", set())
        ctx.setdefault("prev_scored", set()).add(token_id)
        ctx["scored"][token_id] = inp
        ctx["verified"].pop(token_id, None)
        r = inp.get("rotation_score", 0)
        c = inp.get("cleanup_score", 0)
        a = inp.get("security_audit_score", 0)
        return (
            f"Scored token {token_id}: rotation={r}, cleanup={c}, security_audit={a}. "
            f"Call verify_single_token_score({token_id}) to submit for independent audit."
            + notes,
            {"type": "step", "tool": name, "call": f"score_single_token({token_id})",
             "label": f"Scored: {inp.get('token_name', token_id)}",
             "detail": f"rotation={r} | cleanup={c} | audit={a}",
             "rescore": is_rescore},
        )

    if name == "verify_single_token_score":
        token_id = inp.get("token_id")
        score_data = ctx["scored"].get(token_id)
        record = ctx["token_records"].get(token_id)
        if not score_data or not record:
            return (
                f"Cannot verify token {token_id} — score it first.",
                {"type": "step", "tool": name, "label": f"Verify blocked: {token_id} not scored", "detail": ""},
            )

        window_days = round(ctx["window_seconds"] / 86400, 2)
        if ctx.get("auth_mode") == "claude_cli":
            verdict = await run_judge_cli(record, score_data, window_days)
        else:
            verdict = await run_judge(ctx["client"], record, score_data, window_days)

        ctx["verified"][token_id] = verdict
        approved = verdict["approved"]
        judge_error = bool(verdict.get("error"))
        objections = verdict.get("objections", [])
        reasoning = verdict.get("reasoning", "") or ""

        if not approved:
            ctx["reject_counts"][token_id] = ctx["reject_counts"].get(token_id, 0) + 1
        rejects = ctx["reject_counts"].get(token_id, 0)

        if approved:
            result = f"Independent judge APPROVED token {token_id}.\n{reasoning}"
            label = f"Judge APPROVED: {token_id}"
        elif rejects >= MAX_JUDGE_REJECTIONS:
            result = (
                f"Independent judge did NOT approve token {token_id} "
                f"({rejects} failed verdicts — budget spent).\n{reasoning}\n"
                f"Objections: {'; '.join(objections)}\n"
                f"STOP re-scoring this token. Either call emit_recommendation({token_id}, "
                f"recommended_action='insufficient_data') explaining what could not be verified, "
                f"or call clarify_with_user if a human decision would unblock it."
            )
            label = (f"Judge UNAVAILABLE ×{rejects}: {token_id}" if judge_error
                     else f"Judge REJECTED ×{rejects}: {token_id}")
        else:
            result = (
                f"Independent judge REJECTED token {token_id}.\n{reasoning}\n"
                f"Objections: {'; '.join(objections)}\n"
                f"Re-score to address these, then verify again. You have "
                f"{MAX_JUDGE_REJECTIONS - rejects} attempt(s) left before you must fall back to "
                f"'insufficient_data'."
            )
            label = (f"Judge UNAVAILABLE: {token_id}" if judge_error
                     else f"Judge REJECTED: {token_id}")

        return result, {
            "type": "step", "tool": name,
            "call": f"verify_single_token_score({token_id})",
            "label": label,
            "detail": reasoning[:200],
            "loop_event": not approved,
            "judge_error": judge_error,
            "reject_count": rejects,
        }

    if name == "emit_recommendation":
        token_id = inp.get("token_id")
        action = inp.get("recommended_action", "no_action")
        verdict = ctx["verified"].get(token_id)
        if not verdict:
            return (
                f"Cannot emit token {token_id} — it has not been verified. "
                f"Call verify_single_token_score({token_id}) first.",
                {"type": "step", "tool": name, "label": f"Emit blocked: {token_id} unverified", "detail": ""},
            )

        approved = bool(verdict.get("approved"))
        rejects = ctx["reject_counts"].get(token_id, 0)
        # Escape hatch: once the rejection budget is spent, the honest answer may be
        # emitted without a passing verdict — but only that answer.
        budget_spent = rejects >= MAX_JUDGE_REJECTIONS
        escape = (not approved) and budget_spent and action == "insufficient_data"

        if not approved and not escape:
            if budget_spent:
                return (
                    f"Cannot emit '{action}' for token {token_id} — the auditor rejected it {rejects} times. "
                    f"The only action you may commit without a passing verdict is 'insufficient_data'.",
                    {"type": "step", "tool": name,
                     "label": f"Emit blocked: {token_id} needs insufficient_data", "detail": ""},
                )
            return (
                f"Cannot emit token {token_id} — the judge REJECTED it. "
                f"Objections: {'; '.join(verdict.get('objections', []))}. "
                f"Re-score, then re-verify before emitting.",
                {"type": "step", "tool": name, "label": f"Emit blocked: {token_id} rejected by judge", "detail": ""},
            )

        score_data = ctx["scored"].get(token_id, {})
        ctx["recommendations"][token_id] = {
            **score_data,
            "recommended_action": action,
            "recommendation": clean_str(inp.get("recommendation", "")),
            "verification_approved": approved,
            "verification_reasoning": verdict.get("reasoning", ""),
        }
        done_count = len(ctx["recommendations"])
        total = ctx["total_tokens"] or done_count
        remaining = total - done_count
        unverified_note = "" if approved else " (committed WITHOUT a passing verdict — flagged unverified)"
        return (
            f"Recommendation committed for token {token_id}{unverified_note}. "
            f"{done_count}/{total} finalized. "
            + (f"{remaining} remaining." if remaining > 0
               else "All tokens done. Write a brief store-level summary and stop calling tools."),
            {"type": "step", "tool": name,
             "call": f"emit_recommendation({token_id} → {action})",
             "label": f"Recommendation: {inp.get('token_name', token_id)} → {action}"
                      + ("" if approved else " [UNVERIFIED]"),
             "detail": f"{done_count}/{total} done",
             "unverified": not approved},
        )

    # ── Scope ───────────────────────────────────────────────────────────────────

    if name == "record_intent":
        ctx["intent"] = inp
        extracted_sid = inp.get("store_id")
        if extracted_sid and not ctx.get("store_id"):
            ctx["store_id"] = extracted_sid

        request_type   = inp.get("request_type", "general_question")
        requires_rec   = inp.get("requires_recommendation", False)
        open_questions = inp.get("open_questions") or []
        token_ids      = inp.get("token_ids_mentioned") or []
        timeframe      = inp.get("timeframe_hint", "")

        result_parts = [f"Intent recorded: {request_type}. Requires recommendation: {requires_rec}."]

        if token_ids and not ctx.get("store_id"):
            result_parts.append(
                f"Token ID(s) {token_ids} named but no store_id — call lookup_token_store to resolve "
                f"the store yourself. Do not ask the user for it."
            )

        if open_questions:
            # A missing store_id is not blocking when token IDs can resolve it.
            actionable = [q for q in open_questions if not (token_ids and "store_id" in q.lower())]
            if actionable:
                result_parts.append(
                    f"Open questions that must be resolved before proceeding: {'; '.join(actionable)}. "
                    f"Call clarify_with_user now."
                )

        result_parts.append("Nothing has been fetched yet. Decide the next tool yourself.")

        label_parts = [f"Understood: {request_type.replace('_', ' ')}"]
        if timeframe:
            label_parts.append(f"timeframe: {timeframe}")

        return (
            " ".join(result_parts),
            {
                "type": "intent",
                "tool": name,
                "request_type": request_type,
                "store_id": extracted_sid,
                "requires_recommendation": requires_rec,
                "open_questions": open_questions,
                "token_ids_mentioned": token_ids,
                "timeframe_hint": timeframe,
                "label": " — ".join(label_parts),
                "detail": f"requires_recommendation={requires_rec}",
            },
        )

    if name == "lookup_store_tokens":
        conn = ctx.get("conn")
        if conn is None:
            return (
                "Snowflake connection not available — cannot look up tokens. "
                "Ask the user to provide the token IDs directly, or check Snowflake credentials.",
                {"type": "step", "tool": name, "label": "Roster lookup blocked — no Snowflake", "detail": ""},
            )
        store_id = int(inp.get("store_id", ctx.get("store_id", 0)))
        if not store_id:
            return (
                "No store_id provided to lookup_store_tokens.",
                {"type": "step", "tool": name, "label": "Roster lookup blocked — no store_id", "detail": ""},
            )

        _SQL = (
            "SELECT API_TOKEN_ID AS id, NAME AS name, CREATED_AT "
            "FROM API_TOKEN "
            "WHERE STORE_ID = %s AND _FIVETRAN_DELETED = FALSE "
            "ORDER BY CREATED_AT DESC"
        )

        def _run_query():
            cur = conn.cursor()
            cur.execute(_SQL, (store_id,))
            cols = [c[0].lower() for c in cur.description]
            rows = cur.fetchall()
            cur.close()
            result = [dict(zip(cols, row)) for row in rows]
            for t in result:
                if t.get("created_at"):
                    t["created_at"] = str(t["created_at"])
            return result

        try:
            tokens, store_settings = await asyncio.gather(
                asyncio.to_thread(_run_query),
                asyncio.to_thread(usage_mod.fetch_store_settings_sync, conn, store_id),
                return_exceptions=True,
            )
            if isinstance(tokens, Exception):
                raise tokens
            if isinstance(store_settings, dict):
                ctx["store_settings"] = store_settings
        except Exception as e:
            return (
                f"Snowflake query failed: {e}",
                {"type": "step", "tool": name, "label": "Roster lookup error", "detail": str(e)},
            )

        ctx["store_id"]     = store_id
        ctx["tokens"]       = tokens
        ctx["total_tokens"] = len(tokens)

        roster_lines = "\n".join(
            f"  id={t['id']}, name=\"{t['name']}\", created_at={t['created_at']}" for t in tokens
        )
        roster_summary = [{"id": t["id"], "name": t["name"], "created_at": t["created_at"]} for t in tokens]
        settings_note = usage_mod.format_store_settings(ctx.get("store_settings", {}))

        quota_note = ""
        quota_ui = None
        itl = ctx.get("store_settings", {}).get("internal_tokens_limit")
        if itl is not None:
            try:
                itl_int = int(itl)
                n = len(tokens)
                pct = round(n / itl_int * 100) if itl_int else 0
                quota_ui = {"current": n, "limit": itl_int, "pct": pct}
                if pct >= 100:
                    quota_note = (
                        f"CRITICAL — AT TOKEN LIMIT: {n}/{itl_int} tokens ({pct}% quota used). "
                        f"No new tokens can be created — revoke at least one idle token to free a slot.\n"
                    )
                elif pct >= 80:
                    quota_note = f"WARNING — NEAR TOKEN LIMIT: {n}/{itl_int} tokens ({pct}% quota used).\n"
                else:
                    quota_note = f"Token quota: {n}/{itl_int} ({pct}% used).\n"
            except (TypeError, ValueError):
                pass

        return (
            f"Store {store_id} has {len(tokens)} token(s):\n{roster_lines}\n{quota_note}{settings_note}",
            {
                "type": "step",
                "tool": name,
                "call": f"lookup_store_tokens(store_id={store_id})",
                "label": f"Roster: {len(tokens)} token(s) for store {store_id}",
                "detail": f"{len(tokens)} token(s) fetched from Snowflake",
                "roster": roster_summary,
                "token_quota": quota_ui,
            },
        )

    if name == "lookup_token_store":
        conn = ctx.get("conn")
        if conn is None:
            return (
                "Snowflake connection not available — cannot look up token store. "
                "Ask the user which store the token belongs to.",
                {"type": "step", "tool": name, "label": "Token lookup blocked — no Snowflake", "detail": ""},
            )
        token_id = int(inp.get("token_id", 0))
        if not token_id:
            return (
                "No token_id provided to lookup_token_store.",
                {"type": "step", "tool": name, "label": "Token lookup blocked — no token_id", "detail": ""},
            )

        try:
            rec = await asyncio.to_thread(usage_mod.fetch_token_record_sync, conn, token_id)
        except Exception as e:
            return (
                f"Snowflake query failed: {e}",
                {"type": "step", "tool": name, "label": "Token lookup error", "detail": str(e)},
            )

        if rec is None:
            return (
                f"Token {token_id} not found in Snowflake. It may not exist or may have been deleted.",
                {"type": "step", "tool": name, "label": f"Token {token_id} not found", "detail": ""},
            )

        found_store_id = int(rec["store_id"])
        ctx["store_id"] = found_store_id

        # Register the token so score/verify/emit can address it, and so the
        # "N/total finalized" counter is meaningful in free-form runs.
        if rec["id"] not in {t["id"] for t in ctx["tokens"]}:
            ctx["tokens"].append({
                "id": rec["id"], "name": rec["name"], "created_at": rec["created_at"],
            })
            ctx["total_tokens"] = len(ctx["tokens"])

        # The skills score against the store's actual rate limit, so fetch it here
        # rather than making the agent guess a tier.
        settings_note = ""
        try:
            settings = await asyncio.to_thread(
                usage_mod.fetch_store_settings_sync, conn, found_store_id
            )
            if isinstance(settings, dict):
                ctx["store_settings"] = settings
                settings_note = usage_mod.format_store_settings(settings)
        except Exception:
            pass

        return (
            f"Token {token_id} belongs to store {found_store_id} "
            f"(name: \"{rec['name']}\", created_at: {rec['created_at']}).\n{settings_note}"
            f"No usage data has been fetched — call fetch_token_usage with a window you choose.",
            {
                "type": "step",
                "tool": name,
                "call": f"lookup_token_store(token_id={token_id})",
                "label": f"Token {token_id} → store {found_store_id}",
                "detail": f"\"{rec['name']}\" — resolved from Snowflake",
            },
        )

    return f"Unknown tool: {name}", {"type": "step", "tool": name, "label": f"Unknown tool: {name}", "detail": ""}
