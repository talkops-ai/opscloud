"""JSON parsing utilities for plugins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
