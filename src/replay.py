"""LLM-free replay: bind params, resolve locators in order, classify known outcomes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.classify import checkpoint_met, classify_known_outcome, describe_checkpoint
from src.evidence import evidence_dir_for, write_result, write_snapshots
from src.guardrails import Policy, PolicyDenied, assert_navigation_allowed, before_act, load_policy
from src.redact import redact_text
from src.result import ReplayResult
from src.session import WebSession
from src.surface import (
    CssPrimaryForbidden,
    LocatorMiss,
    MissingFrame,
    click,
    extract,
    fill,
    wait_for_outcome_or_checkpoint,
)

logger = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_MS = 15000
CONTENT_FRAME = "content"


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
        result.evidence_dir = str(dest.relative_to(repo_root)) if dest.is_relative_to(repo_root) else str(dest)
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
) -> ReplayResult:
    policy = policy or load_policy(repo_root / "policies" / "allowlist.yaml")
    entry = entry_override or (capability.get("app") or {}).get("entry") or "/"
    start_url = _entry_url(base_url, entry)

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

    blocked_step = None
    try:
        for step in capability.get("steps") or []:
            blocked_step = step.get("id")
            before_act(policy, action=step.get("action") or "", step=step, capability=capability)
    except PolicyDenied as exc:
        return _policy_result(repo_root, exc, failed_step=blocked_step)

    session = WebSession(headed=headed, policy=policy)
    page = None
    outputs: dict[str, Any] = {}
    current_step = None

    try:
        page = session.start()
        session.goto_entry(base_url, entry)

        for step in capability.get("steps") or []:
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
                    result = ReplayResult(
                        status="business_outcome",
                        outcome_code=known["code"],
                        outputs={},
                        failed_step=None,
                        expected=known["observed"],
                        observed=known["observed"],
                    )
                    return _finalize(session, page, repo_root, result)
                key = (step.get("output_binding") or {}).get("key")
                if not key:
                    raise ValueError(f"extract step {current_step} missing output_binding.key")
                outputs[key] = extract(page, target, timeout_ms=timeout_ms)
            else:
                return _finalize(
                    session,
                    page,
                    repo_root,
                    ReplayResult(
                        status="failed",
                        outcome_code="action_not_allowlisted",
                        failed_step=current_step,
                        expected="fill|click|extract",
                        observed=str(action),
                    ),
                )

            for url in session.current_urls():
                assert_navigation_allowed(policy, url)

            known = classify_known_outcome(page, capability)
            if known:
                result = ReplayResult(
                    status="business_outcome",
                    outcome_code=known["code"],
                    outputs={},
                    failed_step=None,
                    expected=known["observed"],
                    observed=known["observed"],
                )
                return _finalize(session, page, repo_root, result)

        if not checkpoint_met(page, capability):
            return _finalize(
                session,
                page,
                repo_root,
                ReplayResult(
                    status="failed",
                    outcome_code="checkpoint_miss",
                    failed_step=current_step,
                    expected=describe_checkpoint(capability),
                    observed="checkpoint not visible",
                    outputs=outputs,
                ),
            )

        result = ReplayResult(
            status="success",
            outcome_code="ok",
            outputs=outputs,
            failed_step=None,
            expected=describe_checkpoint(capability),
            observed=describe_checkpoint(capability),
        )
        return _finalize(session, page, repo_root, result)

    except PolicyDenied as exc:
        return _policy_result(repo_root, exc, failed_step=current_step, session=session, page=page)
    except CssPrimaryForbidden as exc:
        return _finalize(
            session,
            page,
            repo_root,
            ReplayResult(
                status="failed",
                outcome_code="css_primary_forbidden",
                failed_step=current_step,
                expected=exc.expected,
                observed=exc.observed,
            ),
        )
    except MissingFrame as exc:
        return _finalize(
            session,
            page,
            repo_root,
            ReplayResult(
                status="failed",
                outcome_code="missing_frame",
                failed_step=current_step,
                expected=f"iframe name={exc.frame_name!r}",
                observed="frame not attached",
            ),
        )
    except LocatorMiss as exc:
        return _finalize(
            session,
            page,
            repo_root,
            ReplayResult(
                status="failed",
                outcome_code="locator_miss",
                failed_step=current_step,
                expected=exc.expected,
                observed=exc.observed,
            ),
        )
    except PlaywrightTimeoutError as exc:
        return _finalize(
            session,
            page,
            repo_root,
            ReplayResult(
                status="failed",
                outcome_code="timeout",
                failed_step=current_step,
                expected="known outcome or checkpoint visible",
                observed=str(exc),
            ),
        )
    except Exception as exc:
        logger.exception("replay crashed")
        return _finalize(
            session,
            page,
            repo_root,
            ReplayResult(
                status="failed",
                outcome_code="internal_error",
                failed_step=current_step,
                expected="step completed",
                observed=str(exc),
            ),
        )


def _finalize(
    session: WebSession,
    page: Any,
    repo_root: Path,
    result: ReplayResult,
) -> ReplayResult:
    dest = evidence_dir_for(result.status, result.outcome_code, repo_root)
    result.evidence_dir = str(dest.relative_to(repo_root)) if dest.is_relative_to(repo_root) else str(dest)
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
    return result
