"""Small helpers shared across the agent modules."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def clean_str(text: str) -> str:
    text = re.sub(r"</\w[\w\-]*>\s*$", "", text.strip())
    text = re.sub(r"<parameter\b[^>]*>.*$", "", text.strip(), flags=re.DOTALL)
    return text.strip()
