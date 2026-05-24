from __future__ import annotations

from .errors import BoardwrightError


VARIANTS = ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")


def normalize_variant(value: str) -> str:
    variant = value.strip().upper()
    if variant not in VARIANTS:
        allowed = ", ".join(VARIANTS)
        raise BoardwrightError(f"Unsupported variant '{value}'. Use one of: {allowed}.")
    return variant
