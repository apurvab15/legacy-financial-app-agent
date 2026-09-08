"""Classify known banners vs checkpoint. Business outcome is not a crash."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page

from src.surface import detect_visible

CONTENT_FRAME = "content"


def classify_known_outcome(page: Page, capability: dict[str, Any]) -> dict[str, str] | None:
    for outcome in capability.get("known_outcomes") or []:
        detect = outcome.get("detect") or {}
        if detect_visible(page, CONTENT_FRAME, detect, timeout_ms=500):
            code = outcome.get("code") or "unknown_outcome"
            text = detect.get("text") or detect.get("name") or code
            return {"code": code, "observed": str(text)}
    return None


def checkpoint_met(page: Page, capability: dict[str, Any]) -> bool:
    checkpoint = capability.get("checkpoint") or {}
    return detect_visible(page, CONTENT_FRAME, checkpoint, timeout_ms=500)


def describe_checkpoint(capability: dict[str, Any]) -> str:
    checkpoint = capability.get("checkpoint") or {}
    kind = checkpoint.get("kind")
    if kind == "role_name_visible":
        return f"heading {checkpoint.get('name')!r}" if checkpoint.get("role") == "heading" else (
            f"role={checkpoint.get('role')} name={checkpoint.get('name')!r}"
        )
    if kind == "text_visible":
        return f"text {checkpoint.get('text')!r}"
    return str(checkpoint)
