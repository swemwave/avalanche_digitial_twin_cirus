from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def summarize_json(value: Any, depth: int = 0, max_depth: int = 3) -> dict[str, Any]:
    if depth >= max_depth:
        return {"type": type(value).__name__}
    if isinstance(value, Mapping):
        keys = list(value.keys())
        return {
            "type": "object",
            "key_count": len(keys),
            "keys": [str(key) for key in keys[:25]],
            "children": {
                str(key): summarize_json(value[key], depth + 1, max_depth)
                for key in keys[:8]
            },
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        summary: dict[str, Any] = {"type": "array", "length": len(value)}
        if value:
            summary["first"] = summarize_json(value[0], depth + 1, max_depth)
        return summary
    return {"type": type(value).__name__, "sample": value if isinstance(value, (str, int, float, bool)) else None}
