from __future__ import annotations

import argparse

from .constants import REASONING_EFFORTS
from .models import ModelSpec

def parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def parse_percent(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a percentage from 0 to 100") from exc
    if parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError("must be a percentage from 0 to 100")
    return parsed


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def parse_model_spec(value: str) -> ModelSpec:
    raw = value.strip()
    if not raw:
        raise ValueError("model spec cannot be empty")

    if ":" in raw:
        model_part, effort_part = raw.rsplit(":", 1)
    else:
        parts = raw.split()
        if len(parts) < 2:
            raise ValueError(
                "model spec must include a model and reasoning effort, for example: gpt-5.4 xhigh"
            )
        model_part = " ".join(parts[:-1])
        effort_part = parts[-1]

    reasoning_effort = effort_part.strip().lower()
    if reasoning_effort not in REASONING_EFFORTS:
        allowed = ", ".join(sorted(REASONING_EFFORTS))
        raise ValueError(
            f"unsupported reasoning effort {effort_part!r}; expected one of: {allowed}"
        )

    model = normalize_model_name(model_part.strip())
    if not model:
        raise ValueError("model name cannot be empty")

    return ModelSpec(raw=raw, model=model, reasoning_effort=reasoning_effort)


def normalize_model_name(value: str) -> str:
    return "-".join(value.split())
