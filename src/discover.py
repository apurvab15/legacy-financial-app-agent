"""Observe-decide-act discovery loop. AX-tree TEXT in; no screenshots; no RL."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.compiler import artifact_contains_run_id, compile_capability
from src.evidence import write_result, write_snapshots
from src.guardrails import PolicyDenied, before_act, load_policy
from src.hitl import (
    NoOperator,
    Operator,
    write_ax_snapshot,
    write_handoff_record,
    write_intervention_request,
)
from src.llm import SYSTEM_PROMPT, LlmError, ToolCall, complete_turn, load_llm_config
from src.mock_server import ensure_mock
from src.observe import observe_ax_text, target_from_node
from src.redact import dumps_redacted, redact_text
from src.result import ReplayResult
from src.session import WebSession
from src.surface import (
    CssPrimaryForbidden,
    LocatorMiss,
    MissingFrame,
    click,
    extract,
    fill,
)

logger = logging.getLogger(__name__)

CONTENT_FRAME = "content"
MAX_STEPS_DEFAULT = 20
STUB_CAPABILITY = {"risk": "safe"}
# The mock answers Inquiry with a 302, so the iframe navigates twice. Replay waits
# on the capability's checkpoint; discovery has no checkpoint yet, so it waits for
# the frame content itself to change before taking the next observation.
SETTLE_TIMEOUT_MS = 5000
SETTLE_POLL_MS = 100


def _content_fingerprint(page: Any, frame_name: str = CONTENT_FRAME) -> tuple[str, str] | None:
    frame = page.frame(name=frame_name)
    if frame is None:
        return None
    try:
        text = frame.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        # Mid-navigation the execution context is gone; treat as unknown.
        return None
    return frame.url, str(text)


def _wait_for_frame_change(
    page: Any, frame_name: str, before: tuple[str, str] | None, timeout_ms: int
) -> bool:
    """True if the frame changed. False is legitimate: not every click navigates."""
    budget = min(timeout_ms, SETTLE_TIMEOUT_MS)
    waited = 0
    while waited < budget:
        current = _content_fingerprint(page, frame_name)
        if current is not None and current != before:
            frame = page.frame(name=frame_name)
            if frame is not None:
                try:
                    frame.wait_for_load_state("domcontentloaded", timeout=budget - waited)
                except PlaywrightTimeoutError:
                    pass
            return True
        page.wait_for_timeout(SETTLE_POLL_MS)
        waited += SETTLE_POLL_MS
    return False


def run_discovery(
    *,
    goal: str,
    member_id: str,
    base_url: str,
    repo_root: Path,
    headed: bool = False,
    max_steps: int = MAX_STEPS_DEFAULT,
    output_path: Path | None = None,
    timeout_ms: int = 15000,
    operator: Operator | None = None,
) -> dict[str, Any]:
    load_dotenv(repo_root / ".env")
    load_dotenv()
    try:
        llm_config = load_llm_config()
    except LlmError as exc:
        result = ReplayResult(
            status="failed",
            outcome_code="llm_error",
            expected="GEMINI_API_KEY in .env",
            observed=str(exc),
        )
        dest = repo_root / "evidence" / "discovery"
        dest.mkdir(parents=True, exist_ok=True)
        result.evidence_dir = "evidence/discovery"
        write_result(dest, result)
        payload = result.to_dict()
        payload["capability_path"] = None
        return payload
    policy = load_policy(repo_root / "policies" / "allowlist.yaml")
    ensure_mock(base_url)

    dest = repo_root / "evidence" / "discovery"
    dest.mkdir(parents=True, exist_ok=True)
    output_path = output_path or (repo_root / "capabilities" / "lookup-savings-balance.discovered.json")

    session = WebSession(headed=headed, policy=policy)
    page = None
    recorded: list[dict[str, Any]] = []
    outputs: dict[str, Any] = {}
    last_nodes: list[dict[str, Any]] = []
    result = ReplayResult(status="failed", outcome_code="discovery_incomplete")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]

    try:
        page = session.start()
        session.goto_entry(base_url, "/")
        next_ref = 1
        ax_text, refs = observe_ax_text(page, start_index=next_ref)
        next_ref += len(refs)
        last_nodes = list(refs.values())
        user = (
            f"Goal: {goal}\n"
            f"This run's member_id parameter is {member_id}. Fill that into Member ID.\n"
            f"Extract the current savings balance. Then call done.\n\n"
            f"{ax_text}"
        )
        messages.append({"role": "user", "content": user})

        done = False
        for step_i in range(max_steps):
            logger.info("discovery step %s/%s", step_i + 1, max_steps)
            turn = complete_turn(messages, llm_config)
            messages.append(turn.assistant_message)
            if not turn.tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Call a tool: fill, click, extract, done, or escalate.",
                    }
                )
                continue

            for call in turn.tool_calls:
                tool_result, terminal = _apply_tool(
                    call,
                    page=page,
                    session=session,
                    policy=policy,
                    refs=refs,
                    recorded=recorded,
                    outputs=outputs,
                    timeout_ms=timeout_ms,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_result,
                    }
                )
                if terminal == "done":
                    done = True
                    break
                if terminal == "escalate":
                    reason = str(call.arguments.get("reason") or "escalate")
                    _handoff(
                        session,
                        page,
                        dest,
                        repo_root,
                        goal=goal,
                        reason=reason,
                        operator=operator or NoOperator(),
                    )
                    result = ReplayResult(
                        status="escalated",
                        outcome_code="hitl_required",
                        expected="discovery completed",
                        observed=redact_text(reason),
                    )
                    return _finish(
                        session,
                        page,
                        dest,
                        output_path,
                        result,
                        recorded=recorded,
                        last_nodes=last_nodes,
                        member_id=member_id,
                        goal=goal,
                        compile_ok=False,
                    )
            if done:
                break
            ax_text, refs = observe_ax_text(page, start_index=next_ref)
            next_ref += len(refs)
            last_nodes = list(refs.values())
            messages.append({"role": "user", "content": "Updated observation:\n" + ax_text})
        else:
            result = ReplayResult(
                status="failed",
                outcome_code="max_steps",
                expected=f"done within {max_steps} steps",
                observed="max steps reached",
                outputs=outputs,
            )
            return _finish(
                session,
                page,
                dest,
                output_path,
                result,
                recorded=recorded,
                last_nodes=last_nodes,
                member_id=member_id,
                goal=goal,
                compile_ok=False,
            )

        result = ReplayResult(
            status="success",
            outcome_code="ok",
            outputs=outputs,
            expected="heading 'Member Profile'",
            observed="discovery done",
        )
        return _finish(
            session,
            page,
            dest,
            output_path,
            result,
            recorded=recorded,
            last_nodes=last_nodes,
            member_id=member_id,
            goal=goal,
            compile_ok=True,
        )
    except PolicyDenied as exc:
        result = ReplayResult(
            status="failed",
            outcome_code=exc.outcome_code,
            expected=exc.expected,
            observed=exc.observed,
            outputs=outputs,
        )
        return _finish(
            session,
            page,
            dest,
            output_path,
            result,
            recorded=recorded,
            last_nodes=last_nodes,
            member_id=member_id,
            goal=goal,
            compile_ok=False,
        )
    except LlmError as exc:
        result = ReplayResult(
            status="failed",
            outcome_code="llm_error",
            expected="LLM tool call",
            observed=str(exc),
            outputs=outputs,
        )
        return _finish(
            session,
            page,
            dest,
            output_path,
            result,
            recorded=recorded,
            last_nodes=last_nodes,
            member_id=member_id,
            goal=goal,
            compile_ok=False,
        )
    except Exception as exc:
        logger.exception("discovery crashed")
        result = ReplayResult(
            status="failed",
            outcome_code="internal_error",
            expected="discovery completed",
            observed=str(exc),
            outputs=outputs,
        )
        return _finish(
            session,
            page,
            dest,
            output_path,
            result,
            recorded=recorded,
            last_nodes=last_nodes,
            member_id=member_id,
            goal=goal,
            compile_ok=False,
        )


def _handoff(
    session: WebSession,
    page: Any,
    dest: Path,
    repo_root: Path,
    *,
    goal: str,
    reason: str,
    operator: Operator,
) -> None:
    """Discovery escalate -> same-session handoff.

    Discovery does not auto-resume the model loop; replay owns hand-back. With an
    operator attached the prompt holds the browser open so a human can take over.
    """
    try:
        write_snapshots(page, dest)
        write_ax_snapshot(page, dest / "ax_before_handoff.json")
        session.hand_to_human(f"model called escalate: {reason}")
        request = write_intervention_request(
            dest,
            repo_root,
            session_flags=session.flags(),
            goal=goal,
            failed_step="discovery",
            why=f"model called escalate: {reason}",
        )
        logger.warning("intervention request -> %s", dest / "intervention_request.json")
        decision = operator.review(request)
        if decision.action == "resume":
            session.hand_to_automation(decision.note)
            write_ax_snapshot(page, dest / "ax_after_handoff.json")
        write_handoff_record(
            dest,
            session_flags=session.flags(),
            controller_log=session.controller_log,
            operator=operator.name,
            decision=decision,
            reverify=None,
            ax_before=str((dest / "ax_before_handoff.json").relative_to(repo_root)),
            ax_after=(
                str((dest / "ax_after_handoff.json").relative_to(repo_root))
                if decision.action == "resume"
                else None
            ),
        )
    except Exception:
        logger.exception("failed to record discovery handoff")


def _apply_tool(
    call: ToolCall,
    *,
    page: Any,
    session: WebSession,
    policy: Any,
    refs: dict[str, dict[str, Any]],
    recorded: list[dict[str, Any]],
    outputs: dict[str, Any],
    timeout_ms: int,
) -> tuple[str, str | None]:
    name = call.name
    args = call.arguments or {}
    if name == "done":
        return "ok: done", "done"
    if name == "escalate":
        return "ok: escalate", "escalate"
    if name not in {"fill", "click", "extract"}:
        return f"error: unknown tool {name}", None

    ref = str(args.get("ref") or "")
    node = refs.get(ref)
    if node is None:
        return (
            f"error: ref {ref} is not in the current observation "
            f"(valid: {', '.join(sorted(refs)) or 'none'}). Use a ref from the latest observation.",
            None,
        )

    action = name
    target = target_from_node(node, action)
    step = {"id": "discovery", "action": action, "target": target}
    before_act(
        policy,
        action=action,
        step=step,
        capability=STUB_CAPABILITY,
        current_urls=session.current_urls(),
    )

    # A locator miss is the model's mistake, not a crash: report it and let it retry.
    try:
        return _perform(
            action,
            args,
            page=page,
            ref=ref,
            node=node,
            target=target,
            recorded=recorded,
            outputs=outputs,
            timeout_ms=timeout_ms,
        )
    except (LocatorMiss, MissingFrame, CssPrimaryForbidden, PlaywrightTimeoutError) as exc:
        logger.info("tool %s on %s failed: %s", action, ref, exc)
        return (
            f"error: could not {action} {ref} ({type(exc).__name__}). "
            "The page may have changed; pick a control from the latest observation.",
            None,
        )


def _perform(
    action: str,
    args: dict[str, Any],
    *,
    page: Any,
    ref: str,
    node: dict[str, Any],
    target: dict[str, Any],
    recorded: list[dict[str, Any]],
    outputs: dict[str, Any],
    timeout_ms: int,
) -> tuple[str, str | None]:
    if action == "fill":
        text = str(args.get("text") or "")
        fill(page, target, text, timeout_ms=timeout_ms)
        recorded.append({"action": "fill", "node": node, "target": target, "text": text})
        return f"ok: filled {ref}", None
    if action == "click":
        before = _content_fingerprint(page, CONTENT_FRAME)
        click(page, target, timeout_ms=timeout_ms)
        changed = _wait_for_frame_change(page, CONTENT_FRAME, before, timeout_ms)
        recorded.append({"action": "click", "node": node, "target": target})
        if not changed:
            return f"ok: clicked {ref}, but the page content did not change", None
        return f"ok: clicked {ref}, the page changed", None

    key = str(args.get("key") or "") or None
    value = extract(page, target, timeout_ms=timeout_ms)
    if not key:
        nearby = (node.get("nearby") or "").lower()
        key = "savings_balance" if "savings" in nearby or "balance" in nearby else "member_name" if "name" in nearby else "savings_balance"
    outputs[key] = value
    recorded.append({"action": "extract", "node": node, "target": target, "key": key, "text": value})
    return f"ok: extracted {key}={redact_text(value)}", None


def _finish(
    session: WebSession,
    page: Any,
    dest: Path,
    output_path: Path,
    result: ReplayResult,
    *,
    recorded: list[dict[str, Any]],
    last_nodes: list[dict[str, Any]],
    member_id: str,
    goal: str,
    compile_ok: bool,
) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    result.evidence_dir = "evidence/discovery"
    capability_rel = None
    try:
        if page is not None:
            write_snapshots(page, dest)
            ax_text, _ = observe_ax_text(page)
            (dest / "ax_text.txt").write_text(redact_text(ax_text) + "\n", encoding="utf-8")
        (dest / "tool_log.json").write_text(
            dumps_redacted(_public_log(recorded)) + "\n",
            encoding="utf-8",
        )
        if compile_ok and recorded:
            artifact = compile_capability(
                recorded,
                member_id=member_id,
                goal=goal,
                last_nodes=last_nodes,
            )
            if artifact_contains_run_id(artifact, member_id):
                result.status = "failed"
                result.outcome_code = "compiler_leaked_param"
                result.expected = "member_id parameterized, not baked into the skill"
                result.observed = "compiler output contained the discovery member_id"
            else:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
                (dest / "capability.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
                capability_rel = str(output_path)
        session.stop_tracing(dest / "trace.zip")
    except Exception:
        logger.exception("failed to write discovery evidence")
    finally:
        session.close()
    write_result(dest, result)
    payload = result.to_dict()
    payload["capability_path"] = capability_rel
    logger.info("discovery %s %s cap=%s", result.status, result.outcome_code, capability_rel)
    return payload


def _public_log(recorded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for event in recorded:
        node = event.get("node") or {}
        out.append(
            {
                "action": event.get("action"),
                "role": node.get("role"),
                "name": node.get("name"),
                "nearby": node.get("nearby"),
                "key": event.get("key"),
            }
        )
    return out
