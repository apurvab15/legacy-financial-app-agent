"""Human-in-the-loop handoff (CONTRACT.md 5).

The handoff is a flag flip on the *same* live browser session, never a second
Playwright path and never a co-browsing console. Automation stops touching the
page, an operator drives it in the headed window, and on hand-back replay
re-verifies the page before it trusts anything.

The operator console here is deliberately mocked: a stdin prompt (or a scripted
auto-resume for tests). A real deployment would put this behind a queue and an
operator UI; the contract only fixes the intervention request and the flag flip.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from playwright.sync_api import Page

from src.redact import redact_json, redact_text
from src.surface import content_ax, detect_visible

logger = logging.getLogger(__name__)

CONTENT_FRAME = "content"

# Failures a human sitting at the browser can plausibly clear: they are about the
# session or the screen state, not about the capability being wrong.
HUMAN_RECOVERABLE_CODES = frozenset({"session_expired", "locator_miss"})

# Known interstitial. The mock renders this when the Flask session is expired.
SESSION_EXPIRED_TEXT = "Login expired"

OperatorAction = Literal["resume", "abort"]


def detect_session_expired(page: Page, frame_name: str = CONTENT_FRAME) -> bool:
    return detect_visible(
        page, frame_name, {"kind": "text_visible", "text": SESSION_EXPIRED_TEXT}, timeout_ms=500
    )


def classify_blocker(page: Page, fallback_code: str, frame_name: str = CONTENT_FRAME) -> str:
    """A locator miss caused by the login-expired screen is really session_expired."""
    try:
        if detect_session_expired(page, frame_name):
            return "session_expired"
    except Exception:  # noqa: BLE001 - detection is best-effort
        logger.debug("session-expired detection failed", exc_info=True)
    return fallback_code


@dataclass
class OperatorDecision:
    action: OperatorAction
    note: str


# Operators may optionally implement attach(session) to receive the live session
# before review(). A real operator console needs it to show the page; a scripted
# operator uses it to drive the browser the way a human would. Note that driving
# it goes through raw Playwright, not src.surface, so the controller gate still
# refuses every automation act while the human owns the session.
def attach_operator(operator: Any, session: Any) -> None:
    hook = getattr(operator, "attach", None)
    if callable(hook):
        hook(session)


class NoOperator:
    """Default. Nobody is attached, so an escalation just stops and stays escalated."""

    name = "none"

    def review(self, request: dict[str, Any]) -> OperatorDecision:
        logger.warning("no operator attached; session stays with the human")
        return OperatorDecision("abort", "no operator attached")


class AutoResumeOperator:
    """Scripted operator for tests and non-interactive demos."""

    name = "auto"

    def __init__(self, note: str = "auto-resume (scripted operator)") -> None:
        self._note = note

    def review(self, request: dict[str, Any]) -> OperatorDecision:
        logger.warning("auto operator resuming without human input")
        return OperatorDecision("resume", self._note)


class CliOperator:
    """Mocked operator console: print the request, block on stdin."""

    name = "cli"

    def review(self, request: dict[str, Any]) -> OperatorDecision:
        banner = "=" * 68
        sys.stderr.write(
            f"\n{banner}\nOPERATOR INTERVENTION REQUEST\n{banner}\n"
            f"{json.dumps(request, indent=2)}\n{banner}\n"
            "The browser is open and automation is paused. Fix the screen by hand.\n"
            "Then type 'resume' to hand control back, or 'abort' to stop.\n"
        )
        sys.stderr.flush()
        while True:
            sys.stderr.write("operator> ")
            sys.stderr.flush()
            try:
                raw = sys.stdin.readline()
            except KeyboardInterrupt:
                return OperatorDecision("abort", "operator interrupted")
            if not raw:
                return OperatorDecision("abort", "operator stream closed")
            answer = raw.strip().lower()
            if answer in {"resume", "r", "continue"}:
                note = _prompt_note()
                return OperatorDecision("resume", note or "operator resumed")
            if answer in {"abort", "a", "stop", "quit"}:
                return OperatorDecision("abort", "operator aborted")
            sys.stderr.write("expected 'resume' or 'abort'\n")


def _prompt_note() -> str:
    sys.stderr.write("note (what did you do, optional): ")
    sys.stderr.flush()
    try:
        return (sys.stdin.readline() or "").strip()
    except KeyboardInterrupt:
        return ""


Operator = NoOperator | AutoResumeOperator | CliOperator


def build_operator(kind: str, note: str | None = None) -> Operator:
    name = (kind or "none").strip().lower()
    if name == "cli":
        return CliOperator()
    if name == "auto":
        return AutoResumeOperator(note) if note else AutoResumeOperator()
    return NoOperator()


def _rel(path: Path, repo_root: Path) -> str:
    return str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path)


def write_intervention_request(
    dest: Path,
    repo_root: Path,
    *,
    session_flags: dict[str, Any],
    goal: str,
    failed_step: str | None,
    why: str,
) -> dict[str, Any]:
    """CONTRACT.md 5.2. Note the absence of any screenshot field."""
    request = {
        "session_id": session_flags.get("session_id"),
        "goal": goal,
        "failed_step": failed_step,
        "why": why,
        "ax_snapshot_path": _rel(dest / "ax.json", repo_root),
        "dom_snapshot_path": _rel(dest / "dom.html", repo_root),
        "controller": session_flags.get("controller"),
    }
    redacted = redact_json(request)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "intervention_request.json").write_text(
        json.dumps(redacted, indent=2) + "\n", encoding="utf-8"
    )
    return redacted


def write_ax_snapshot(page: Page, path: Path, frame_name: str = CONTENT_FRAME) -> None:
    """AX-only snapshot of the human's before/after state. No pixels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content_ax(page, frame_name)
    path.write_text(json.dumps(redact_json(payload), indent=2) + "\n", encoding="utf-8")


def write_handoff_record(
    dest: Path,
    *,
    session_flags: dict[str, Any],
    controller_log: list[dict[str, str]],
    operator: str,
    decision: OperatorDecision | None,
    reverify: str | None,
    ax_before: str | None,
    ax_after: str | None,
) -> None:
    record = {
        "session_id": session_flags.get("session_id"),
        "operator": operator,
        "decision": decision.action if decision else None,
        "operator_note": redact_text(decision.note) if decision else None,
        "reverify": reverify,
        "controller_flips": controller_log,
        "final_controller": session_flags.get("controller"),
        "ax_before_handoff": ax_before,
        "ax_after_handoff": ax_after,
    }
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "handoff.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
