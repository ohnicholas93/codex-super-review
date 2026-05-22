from __future__ import annotations

from typing import Any

def _stringify_error(value: Any) -> str:
    if isinstance(value, dict) and "message" in value:
        return str(value["message"])
    return str(value)


def _get_nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
