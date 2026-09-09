"""LLM-free replay: bind params, resolve locators in order, classify known outcomes.

Phase 5 adds HITL: a blocked run can hand the live session to an operator and,
on hand-back, re-verify the page before it continues. Replay still calls no LLM.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.classify import checkpoint_met, classify_known_outcome, describe_checkpoint
from src.evidence import evidence_dir_for, write_result, write_snapshots
from src.guardrails import (
    HumanRequired,
    Policy,
    PolicyDenied,
    assert_navigation_allowed,
    before_act,
    load_policy,
)
from src.hitl import (
    HUMAN_RECOVERABLE_CODES,
    NoOperator,
    Operator,
    OperatorDecision,
    attach_operator,
    classify_blocker,
    detect_session_expired,
    write_ax_snapshot,
    write_handoff_record,
    write_intervention_request,
)
from src.redact import redact_text
from src.result import ReplayResult
from src.session import WebSession
from src.surface import (
    ControllerLocked,
    CssPrimaryForbidden,
    LocatorMiss,
    MissingFrame,
    click,
    extract,
    fill,
    resolve_target,
    wait_for_outcome_or_checkpoint,
)

logger = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_MS = 15000
CONTENT_FRAME = "content"
REVERIFY_TIMEOUT_MS = 2000


@dataclass
class _Blocker:
    """A stop that has not yet been classified as failed vs escalated."""

    outcome_code: str
    expected: str
    observed: str
    step_id: str | None
    step_index: int | None
    # Whether automation may re-attempt the step after a human clears the way.
    # False for irreversible steps: a human owns those, automation never retries.
    automation_may_retry: bool = True


def load_capability(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("apiVersion") != "capability.interface.ai.local/v1":
        raise ValueError(f"unsupported apiVersion: {data.get('apiVersion')}")
    return data


def origin_allowed(base_url: str, allowlist: list[str]) -> bool:
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    scheme = parsed.scheme
    for allowed in allowlist:
        item = urlparse(allowed)
        if item.hostname == host and item.scheme == scheme:
            if item.port is None or item.port == parsed.port:
                return True
    return False


def _bind_input(step: dict[str, Any], params: dict[str, Any]) -> str:
    binding = step.get("input_binding") or {}
    if binding.get("from") != "input":
        raise ValueError(f"unsupported input_binding: {binding}")
    key = binding.get("key")
    if key not in params:
        raise ValueError(f"missing param {key!r}")
    return str(params[key])


def _entry_url(base_url: str, entry: str) -> str:
    if entry == "/":
        return base_url.rstrip("/") + "/"
    return urljoin(base_url.rstrip("/") + "/", entry.lstrip("/"))


def default_goal(capability: dict[str, Any], params: dict[str, Any]) -> str:
    base = (
        capability.get("description")
        or capability.get("name")
        or capability.get("id")
        or "capability run"
    )
    if params:
        bits = ", ".join(f"{key}={value}" for key, value in sorted(params.items()))
        return f"{base} (params: {bits})"
    return str(base)


def _policy_result(
    repo_root: Path,
    exc: PolicyDenied,
    *,
    failed_step: str | None = None,
    session: WebSession | None = None,
    page: Any = None,
) -> ReplayResult:
    result = ReplayResult(
        status="failed",
        outcome_code=exc.outcome_code,
        failed_step=failed_step,
        expected=exc.expected,
        observed=exc.observed,
    )
    if session is None:
        dest = evidence_dir_for(result.status, result.outcome_code, repo_root)
        result.evidence_dir = (
            str(dest.relative_to(repo_root)) if dest.is_relative_to(repo_root) else str(dest)
        )
        write_result(dest, result)
        logger.info("result %s %s -> %s", result.status, result.outcome_code, result.evidence_dir)
        return result
    return _finalize(session, page, repo_root, result)


def run_replay(
    *,
    capability: dict[str, Any],
    params: dict[str, Any],
    base_url: str,
    repo_root: Path,
    headed: bool = False,
    timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    entry_override: str | None = None,
    policy: Policy | None = None,
    operator: Operator | None = None,
    goal: str | None = None,
    max_handoffs: int = 1,
) -> ReplayResult:
    policy = policy or load_policy(repo_root / "policies" / "allowlist.yaml")
    operator = operator or NoOperator()
    entry = entry_override or (capability.get("app") or {}).get("entry") or "/"
    start_url = _entry_url(base_url, entry)
    goal = goal or default_goal(capability, params)

    try:
        assert_navigation_allowed(policy, start_url)
        cap_origins = (capability.get("app") or {}).get("origin_allowlist") or []
        if cap_origins and not origin_allowed(base_url, cap_origins):
            raise PolicyDenied(
                "origin_not_allowlisted",
                expected=f"origin in {cap_origins}",
                observed=redact_text(base_url),
            )
    except PolicyDenied as exc:
        return _policy_result(repo_root, exc, failed_step=None)

    # Preflight: hard blocks stop before a browser is launched. require_human is
    # deferred, because handing over needs a live session.
    blocked_step = None
    try:
        for step in capability.get("steps") or []:
            blocked_step = step.get("id")
            before_act(
                policy,
                action=step.get("action") or "",
                step=step,
                capability=capability,
                preflight=True,
            )
    except PolicyDenied as exc:
        return _policy_result(repo_root, exc, failed_step=blocked_step)

    session = WebSession(headed=headed, policy=policy)
    page = None
    outputs: dict[str, Any] = {}
    handoffs = 0
    escalated_steps: set[str | None] = set()
    decision: OperatorDecision | None = None
    reverify_verdict: str | None = None
    hitl_dir: Path | None = None
    ax_before_rel: str | None = None
    ax_after_rel: str | None = None
    start_index = 0

    try:
        page = session.start()
        session.goto_entry(base_url, entry)

        while True:
            result, blocker = _attempt_steps(
                start_index,
                session=session,
                page=page,
                capability=capability,
                params=params,
                policy=policy,
                outputs=outputs,
                timeout_ms=timeout_ms,
                repo_root=repo_root,
            )
            if result is not None:
                return _finalize(session, page, repo_root, result)

            assert blocker is not None
            # Policy-driven escalation happens whether or not an operator is attached:
            # the run is escalated and the session belongs to a human either way.
            # Human-recoverable failures only escalate when there is someone to ask,
            # so a plain replay keeps returning failed (CONTRACT.md 3.3).
            must_escalate = blocker.outcome_code == "irreversible_blocked"
            may_escalate = (
                blocker.outcome_code in HUMAN_RECOVERABLE_CODES
                and not isinstance(operator, NoOperator)
            )
            fresh = handoffs < max_handoffs and blocker.step_id not in escalated_steps
            if not (must_escalate or may_escalate) or not fresh:
                return _finalize(
                    session,
                    page,
                    repo_root,
                    _blocker_result(blocker, outputs, escalate=must_escalate),
                )

            # --- automation -> human -------------------------------------------
            handoffs += 1
            escalated_steps.add(blocker.step_id)
            hitl_dir = evidence_dir_for("escalated", blocker.outcome_code, repo_root)
            write_snapshots(page, hitl_dir)
            write_ax_snapshot(page, hitl_dir / "ax_before_handoff.json")
            ax_before_rel = _rel(hitl_dir / "ax_before_handoff.json", repo_root)
            session.hand_to_human(f"{blocker.outcome_code}: {blocker.observed}")
            request = write_intervention_request(
                hitl_dir,
                repo_root,
                session_flags=session.flags(),
                goal=goal,
                failed_step=blocker.step_id,
                why=f"{blocker.outcome_code}: {blocker.observed}",
            )
            logger.warning("intervention request -> %s", _rel(hitl_dir, repo_root))

            attach_operator(operator, session)
            decision = operator.review(request)

            if decision.action != "resume":
                write_handoff_record(
                    hitl_dir,
                    session_flags=session.flags(),
                    controller_log=session.controller_log,
                    operator=operator.name,
                    decision=decision,
                    reverify=None,
                    ax_before=ax_before_rel,
                    ax_after=None,
                )
                return _finalize(
                    session,
                    page,
                    repo_root,
                    _blocker_result(blocker, outputs, escalate=True),
                )

            # --- human -> automation, then re-verify before trusting anything ---
            session.hand_to_automation(decision.note)
            write_ax_snapshot(page, hitl_dir / "ax_after_handoff.json")
            ax_after_rel = _rel(hitl_dir / "ax_after_handoff.json", repo_root)

            reverify_verdict, terminal = _reverify(
                page,
                capability=capability,
                blocker=blocker,
                outputs=outputs,
                timeout_ms=timeout_ms,
            )
            logger.warning("re-verify after resume: %s", reverify_verdict)

            # Re-verification says automation still cannot proceed, so the session
            # goes back to the human rather than pressing on. Flip before recording,
            # so handoff.json reports the controller the run actually ended on.
            if terminal is None and reverify_verdict != "retry_step":
                session.hand_to_human(f"re-verify failed: {reverify_verdict}")

            write_handoff_record(
                hitl_dir,
                session_flags=session.flags(),
                controller_log=session.controller_log,
                operator=operator.name,
                decision=decision,
                reverify=reverify_verdict,
                ax_before=ax_before_rel,
                ax_after=ax_after_rel,
            )

            if terminal is not None:
                return _finalize(session, page, repo_root, terminal)
            if reverify_verdict == "retry_step" and blocker.step_index is not None:
                start_index = blocker.step_index
                continue
            return _finalize(
                session,
                page,
                repo_root,
                _blocker_result(blocker, outputs, escalate=True, observed_override=reverify_verdict),
            )

    except PolicyDenied as exc:
        return _policy_result(repo_root, exc, failed_step=None, session=session, page=page)
    except Exception as exc:
        logger.exception("replay crashed")
        return _finalize(
            session,
            page,
            repo_root,
            ReplayResult(
                status="failed",
                outcome_code="internal_error",
                failed_step=None,
                expected="step completed",
                observed=str(exc),
                outputs=outputs,
            ),
        )


def _rel(path: Path, repo_root: Path) -> str:
    return str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path)


def business_outcome_result(known: dict[str, str]) -> ReplayResult:
    """A classified banner is a typed result, not a crash (CONTRACT.md 3.1)."""
    return ReplayResult(
        status="business_outcome",
        outcome_code=known["code"],
        outputs={},
        failed_step=None,
        expected=known["observed"],
        observed=known["observed"],
    )


def _blocker_result(
    blocker: _Blocker,
    outputs: dict[str, Any],
    *,
    escalate: bool,
    observed_override: str | None = None,
) -> ReplayResult:
    observed = blocker.observed
    if observed_override:
        observed = f"{blocker.observed} (after resume: {observed_override})"
    return ReplayResult(
        status="escalated" if escalate else "failed",
        outcome_code=blocker.outcome_code,
        failed_step=blocker.step_id,
        expected=blocker.expected,
        observed=observed,
        outputs=outputs,
    )


def _attempt_steps(
    start_index: int,
    *,
    session: WebSession,
    page: Any,
    capability: dict[str, Any],
    params: dict[str, Any],
    policy: Policy,
    outputs: dict[str, Any],
    timeout_ms: int,
    repo_root: Path,
) -> tuple[ReplayResult | None, _Blocker | None]:
    """Run steps from start_index. Returns a terminal result or a blocker."""
    steps = capability.get("steps") or []
    current_step: str | None = None
    current_index: int | None = None

    try:
        for index in range(start_index, len(steps)):
            step = steps[index]
            current_index = index
            current_step = step.get("id")
            action = step.get("action")
            target = step.get("target") or {}
            logger.info("step %s %s", current_step, action)
            before_act(
                policy,
                action=action or "",
                step=step,
                capability=capability,
                current_urls=session.current_urls(),
            )

            if action == "fill":
                fill(page, target, _bind_input(step, params), timeout_ms=timeout_ms)
            elif action == "click":
                click(page, target, timeout_ms=timeout_ms)
                wait_for_outcome_or_checkpoint(
                    page,
                    target.get("frame") or CONTENT_FRAME,
                    capability.get("known_outcomes") or [],
                    capability.get("checkpoint") or {},
                    timeout_ms=timeout_ms,
                )
            elif action == "extract":
                known = classify_known_outcome(page, capability)
                if known:
                    return business_outcome_result(known), None
                key = (step.get("output_binding") or {}).get("key")
                if not key:
                    raise ValueError(f"extract step {current_step} missing output_binding.key")
                outputs[key] = extract(page, target, timeout_ms=timeout_ms)
            else:
                return (
                    ReplayResult(
                        status="failed",
                        outcome_code="action_not_allowlisted",
                        failed_step=current_step,
                        expected="fill|click|extract",
                        observed=str(action),
                    ),
                    None,
                )

            for url in session.current_urls():
                assert_navigation_allowed(policy, url)

            known = classify_known_outcome(page, capability)
            if known:
                return business_outcome_result(known), None

        if not checkpoint_met(page, capability):
            return (
                ReplayResult(
                    status="failed",
                    outcome_code="checkpoint_miss",
                    failed_step=current_step,
                    expected=describe_checkpoint(capability),
                    observed="checkpoint not visible",
                    outputs=outputs,
                ),
                None,
            )

        return (
            ReplayResult(
                status="success",
                outcome_code="ok",
                outputs=outputs,
                failed_step=None,
                expected=describe_checkpoint(capability),
                observed=describe_checkpoint(capability),
            ),
            None,
        )

    except HumanRequired as exc:
        return None, _Blocker(
            outcome_code=exc.outcome_code,
            expected=exc.expected,
            observed=exc.observed,
            step_id=current_step,
            step_index=current_index,
            automation_may_retry=False,
        )
    except ControllerLocked as exc:
        return None, _Blocker(
            outcome_code="controller_locked",
            expected=exc.expected,
            observed=exc.observed,
            step_id=current_step,
            step_index=current_index,
        )
    except CssPrimaryForbidden as exc:
        return None, _Blocker(
            outcome_code="css_primary_forbidden",
            expected=exc.expected,
            observed=exc.observed,
            step_id=current_step,
            step_index=current_index,
        )
    except MissingFrame as exc:
        return None, _Blocker(
            outcome_code="missing_frame",
            expected=f"iframe name={exc.frame_name!r}",
            observed="frame not attached",
            step_id=current_step,
            step_index=current_index,
        )
    except LocatorMiss as exc:
        # The login-expired screen shows up as a locator miss; name it honestly.
        code = classify_blocker(page, "locator_miss")
        observed = exc.observed
        if code == "session_expired":
            observed = "Login expired banner in frame 'content'"
        return None, _Blocker(
            outcome_code=code,
            expected=exc.expected,
            observed=observed,
            step_id=current_step,
            step_index=current_index,
        )
    except PlaywrightTimeoutError as exc:
        code = classify_blocker(page, "timeout")
        return None, _Blocker(
            outcome_code=code,
            expected="known outcome or checkpoint visible",
            observed="Login expired banner in frame 'content'"
            if code == "session_expired"
            else str(exc),
            step_id=current_step,
            step_index=current_index,
        )


def _reverify(
    page: Any,
    *,
    capability: dict[str, Any],
    blocker: _Blocker,
    outputs: dict[str, Any],
    timeout_ms: int,
) -> tuple[str, ReplayResult | None]:
    """Never resume on trust. Check the page before continuing (CONTRACT.md 5)."""
    known = classify_known_outcome(page, capability)
    if known:
        return "known_outcome_visible", business_outcome_result(known)

    if checkpoint_met(page, capability):
        # The operator finished the goal by hand.
        return "checkpoint_met", ReplayResult(
            status="success",
            outcome_code="ok",
            outputs=outputs,
            failed_step=None,
            expected=describe_checkpoint(capability),
            observed=f"{describe_checkpoint(capability)} (completed during handoff)",
        )

    if not blocker.automation_may_retry:
        return "human_owns_step", None

    if blocker.outcome_code == "session_expired" and detect_session_expired(page):
        return "still_session_expired", None

    step = _step_by_index(capability, blocker.step_index)
    if step is None:
        return "unknown_step", None
    if not _target_resolves(page, step, timeout_ms):
        return "step_target_still_missing", None
    return "retry_step", None


def _step_by_index(capability: dict[str, Any], index: int | None) -> dict[str, Any] | None:
    steps = capability.get("steps") or []
    if index is None or index < 0 or index >= len(steps):
        return None
    return steps[index]


def _target_resolves(page: Any, step: dict[str, Any], timeout_ms: int) -> bool:
    target = step.get("target") or {}
    action = step.get("action") or "click"
    try:
        resolve_target(page, target, action, timeout_ms=min(REVERIFY_TIMEOUT_MS, timeout_ms))
    except (LocatorMiss, MissingFrame):
        return False
    return True


def _finalize(
    session: WebSession,
    page: Any,
    repo_root: Path,
    result: ReplayResult,
) -> ReplayResult:
    dest = evidence_dir_for(result.status, result.outcome_code, repo_root)
    result.evidence_dir = (
        str(dest.relative_to(repo_root)) if dest.is_relative_to(repo_root) else str(dest)
    )
    try:
        if page is not None:
            write_snapshots(page, dest)
        session.stop_tracing(dest / "trace.zip")
    except Exception:
        logger.exception("failed to write evidence")
    finally:
        session.close()
    write_result(dest, result)
    logger.info("result %s %s -> %s", result.status, result.outcome_code, result.evidence_dir)
    logger.info("controller flips: %s", session.controller_log or "none")
    return result
