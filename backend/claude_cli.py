"""`claude -p` transport — an alternative to the API key, using the local CLI session.

Same loop, different wire format: tool definitions are embedded in the prompt and
the model signals a call with a trailing TOOL_CALL_JSON: line.
"""

import asyncio
import json
import os
import subprocess
import uuid

from config import MODEL

TOOL_USE_MARKER = "TOOL_CALL_JSON:"


async def call_claude_cli(prompt: str, model: str = None) -> str:
    """Call `claude -p` subprocess, stripping API key env vars so session auth is used."""
    use_model = model or MODEL
    cmd = ["claude", "-p", "--output-format", "json", "--allowed-tools", "", "--model", use_model]
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, input=prompt, capture_output=True, text=True, timeout=180, env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude CLI call timed out after 180s")
    except FileNotFoundError:
        raise RuntimeError("'claude' CLI not found — install Claude Code and run `claude login`.")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"claude CLI exited with code {proc.returncode}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Unexpected CLI output: {proc.stdout[:200]}")
    if envelope.get("is_error"):
        raise RuntimeError(envelope.get("result", "CLI error"))
    return envelope.get("result", "")


def build_cli_prompt(system: str, messages: list, tools: list, force_tool: str = None) -> str:
    """Build a plain-text prompt for CLI mode with embedded tool definitions."""
    lines = [system.strip(), ""]

    if tools:
        tool_json = json.dumps(tools, indent=2)
        lines += [
            "=" * 70,
            "## AVAILABLE TOOLS",
            "",
            tool_json,
            "",
            "## TOOL CALL PROTOCOL",
            "",
            "To call a tool, end your response with exactly this on a new line:",
            f"  {TOOL_USE_MARKER} {{\"name\": \"tool_name\", \"input\": {{...}}}}",
            "",
            "Rules: only ONE tool call per response; no text after the JSON; "
            "if done (no tool needed), respond normally without the marker.",
            "=" * 70,
            "",
        ]

    if force_tool:
        lines += [
            f"IMPORTANT: You MUST call the `{force_tool}` tool in this response.",
            "",
        ]

    lines.append("## CONVERSATION")
    lines.append("")

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"### {role.upper()}")
            lines.append(content)
            lines.append("")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    lines.append(f"### {role.upper()}")
                    lines.append(block.get("text", ""))
                    lines.append("")
                elif btype == "thinking":
                    pass
                elif btype == "tool_use":
                    lines.append(f"### ASSISTANT (tool call: {block.get('name')})")
                    lines.append(f"Input: {json.dumps(block.get('input', {}))}")
                    lines.append("")
                elif btype == "tool_result":
                    lines.append("### TOOL RESULT")
                    lines.append(str(block.get("content", "")))
                    lines.append("")

    lines.append("### ASSISTANT")
    return "\n".join(lines)


def parse_cli_response(response: str, known_tools: set) -> tuple:
    """Parse CLI response into (text_or_None, tool_call_or_None)."""
    marker_pos = response.rfind(TOOL_USE_MARKER)
    if marker_pos == -1:
        return response.strip(), None

    pre_text = response[:marker_pos].strip()
    call_json_text = response[marker_pos + len(TOOL_USE_MARKER):].strip()

    try:
        call_data = json.loads(call_json_text)
        name = call_data.get("name", "")
        inp = call_data.get("input", {})
        if name in known_tools:
            tool_call = {
                "type": "tool_use",
                "id": f"cli_{uuid.uuid4().hex[:12]}",
                "name": name,
                "input": inp,
            }
            return pre_text or None, tool_call
    except (json.JSONDecodeError, AttributeError):
        pass

    return response.strip(), None
