"""Write Playwright trace + AX/DOM snapshots. No screenshots. Redact secrets/digit runs."""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page

from src.redact import dumps_redacted, redact_json, redact_text
from src.result import ReplayResult
from src.surface import content_ax, content_dom


def evidence_dir_for(status: str, outcome_code: str, root: Path) -> Path:
    if status == "success":
        name = "replay-success"
    elif status == "business_outcome" and outcome_code == "member_not_found":
        name = "replay-not-found"
    else:
        name = f"replay-{outcome_code or status}"
    dest = root / "evidence" / name
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def write_snapshots(page: Page, dest: Path, frame_name: str = "content") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "dom.html").write_text(redact_text(content_dom(page, frame_name)), encoding="utf-8")
    (dest / "ax.json").write_text(dumps_redacted(content_ax(page, frame_name)) + "\n", encoding="utf-8")


def write_result(dest: Path, result: ReplayResult) -> None:
    payload = result.to_dict()
    outputs = payload.get("outputs")
    redacted = redact_json(payload)
    redacted["outputs"] = outputs
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "result.json").write_text(json.dumps(redacted, indent=2) + "\n", encoding="utf-8")
