"""Phase 5 checks that need no human at the keyboard.

The headed CLI demo is the real deliverable; this exists so the handoff, the
re-verification and the controller gate can be proven without a person clicking.
Run: .venv/bin/python3 scripts/verify_phase5.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.guardrails import load_policy  # noqa: E402
from src.hitl import AutoResumeOperator, OperatorDecision  # noqa: E402
from src.mock_server import ensure_mock  # noqa: E402
from src.replay import load_capability, run_replay  # noqa: E402
from src.surface import ControllerLocked, click  # noqa: E402

BASE_URL = "http://127.0.0.1:8000"
EXPIRED_ENTRY = "/?expired=1"
LOOKUP = REPO_ROOT / "capabilities" / "lookup-savings-balance.json"
SUB_ACCOUNT = REPO_ROOT / "capabilities" / "open-sub-account.json"
POLICY = REPO_ROOT / "policies" / "allowlist.yaml"
POLICY_HITL = REPO_ROOT / "policies" / "allowlist-hitl.yaml"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


class ScriptedHuman:
    """Stands in for the operator in the headed window.

    Clicks Restore session through raw Playwright, exactly as a person would use
    the mouse: it does not go through src.surface, so it is not automation acting.
    """

    name = "scripted-human"

    def __init__(self) -> None:
        self.session = None
        self.gate_refused_automation = False

    def attach(self, session) -> None:
        self.session = session

    def review(self, request) -> OperatorDecision:
        page = self.session.page

        # Requirement 3: while the human holds the session, automation must be refused.
        try:
            click(
                page,
                {
                    "frame": "content",
                    "strategies": [{"strategy": "role", "role": "button", "name": "Restore session"}],
                },
                timeout_ms=2000,
            )
        except ControllerLocked:
            self.gate_refused_automation = True

        # Now the "human" clears the screen by hand.
        frame = page.frame_locator('iframe[name="content"]')
        frame.get_by_role("button", name="Restore session").click()
        content = page.frame(name="content")
        if content is not None:
            content.wait_for_load_state("domcontentloaded")
        return OperatorDecision("resume", "cleared Login expired via Restore session")


class LazyHuman:
    """Resumes without fixing anything, to prove re-verification is not blind."""

    name = "lazy-human"

    def review(self, request) -> OperatorDecision:
        return OperatorDecision("resume", "resumed without clearing the banner")


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ensure_mock(BASE_URL)
    # Evidence paths are derived from outcome_code, so several checks here collide
    # with each other and with the curated runs in evidence/. Write to scratch.
    evidence_root = Path(tempfile.mkdtemp(prefix="phase5-evidence-"))
    print(f"evidence for this run: {evidence_root}")
    policy = load_policy(POLICY)
    policy_hitl = load_policy(POLICY_HITL)
    lookup = load_capability(LOOKUP)
    sub_account = load_capability(SUB_ACCOUNT)

    print("\n[1] session_expired -> escalate -> human clears it -> resume -> completes")
    human = ScriptedHuman()
    result = run_replay(
        capability=lookup,
        params={"member_id": "12345"},
        base_url=BASE_URL,
        repo_root=evidence_root,
        entry_override=EXPIRED_ENTRY,
        policy=policy,
        operator=human,
    )
    check("status is success after hand-back", result.status == "success", result.status)
    check(
        "savings_balance extracted after resume",
        result.outputs.get("savings_balance") == "1,240.55",
        str(result.outputs),
    )
    check(
        "controller gate refused automation while human held session",
        human.gate_refused_automation,
    )
    flips = [entry["controller"] for entry in (human.session.controller_log or [])]
    check("controller flipped human then automation", flips == ["human", "automation"], str(flips))

    print("\n[2] operator resumes without fixing -> re-verify refuses to continue")
    result = run_replay(
        capability=lookup,
        params={"member_id": "12345"},
        base_url=BASE_URL,
        repo_root=evidence_root,
        entry_override=EXPIRED_ENTRY,
        policy=policy,
        operator=LazyHuman(),
    )
    check("status is escalated", result.status == "escalated", result.status)
    check("outcome_code is session_expired", result.outcome_code == "session_expired", result.outcome_code)
    check(
        "observed records the failed re-verification",
        "still_session_expired" in result.observed,
        result.observed,
    )

    print("\n[3] no operator attached -> locator_miss stays failed (Phase 2 matrix)")
    result = run_replay(
        capability=lookup,
        params={"member_id": "12345"},
        base_url=BASE_URL,
        repo_root=evidence_root,
        entry_override=EXPIRED_ENTRY,
        policy=policy,
    )
    check("status is failed", result.status == "failed", result.status)
    check("outcome_code is session_expired", result.outcome_code == "session_expired", result.outcome_code)

    print("\n[4] irreversible require_human -> escalated, Confirm open never clicked")
    result = run_replay(
        capability=sub_account,
        params={},
        base_url=BASE_URL,
        repo_root=evidence_root,
        policy=policy_hitl,
        operator=AutoResumeOperator("operator reviewed and declined to open the account"),
    )
    check("status is escalated", result.status == "escalated", result.status)
    check(
        "outcome_code is irreversible_blocked",
        result.outcome_code == "irreversible_blocked",
        result.outcome_code,
    )
    # The mock renders "Not authorized" only if /sub-account/confirm was POSTed.
    dom = (evidence_root / result.evidence_dir / "dom.html").read_text(encoding="utf-8")
    check("automation never reached the confirm POST", "Not authorized" not in dom)

    print("\n[5] irreversible mode=block is unchanged (Phase 3)")
    result = run_replay(
        capability=sub_account,
        params={},
        base_url=BASE_URL,
        repo_root=evidence_root,
        policy=policy,
    )
    check("status is failed", result.status == "failed", result.status)
    check(
        "outcome_code is irreversible_blocked",
        result.outcome_code == "irreversible_blocked",
        result.outcome_code,
    )

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
