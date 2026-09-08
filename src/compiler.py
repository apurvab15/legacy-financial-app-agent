"""Compile a successful discovery trace into the locked capability schema. Drop the transcript."""

from __future__ import annotations

from typing import Any

from src.observe import target_from_node


def compile_capability(
    recorded: list[dict[str, Any]],
    *,
    member_id: str,
    goal: str,
    last_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    saw_inquiry = False
    heading_name = "Member Profile"
    last_nodes = last_nodes or []

    for node in last_nodes:
        if node.get("role") == "heading" and node.get("name"):
            heading_name = str(node["name"])
            break

    index = 1
    for event in recorded:
        action = event.get("action")
        if action not in {"fill", "click", "extract"}:
            continue
        node = event.get("node") or {}
        target = event.get("target") or target_from_node(node, action)
        if not target.get("strategies"):
            continue
        step: dict[str, Any] = {
            "id": f"s{index}",
            "action": action,
            "target": {
                "frame": target.get("frame") or "content",
                "strategies": target["strategies"],
            },
        }
        if action == "fill":
            typed = str(event.get("text") or "")
            if typed.strip() == str(member_id).strip():
                step["input_binding"] = {"from": "input", "key": "member_id"}
            else:
                # Do not bake run-specific ids; skip non-parameter fills.
                continue
        elif action == "click":
            name = (node.get("name") or "").lower()
            if "inquiry" in name:
                saw_inquiry = True
        elif action == "extract":
            key = event.get("key") or _infer_output_key(node)
            step["output_binding"] = {"key": key}
        steps.append(step)
        index += 1

    if saw_inquiry and not any(s.get("action") == "extract" for s in steps):
        extract_node = next(
            (n for n in last_nodes if "savings" in (n.get("nearby") or "").lower()),
            None,
        )
        if extract_node:
            target = target_from_node(extract_node, "extract")
            steps.append(
                {
                    "id": f"s{index}",
                    "action": "extract",
                    "target": target,
                    "output_binding": {"key": "savings_balance"},
                }
            )

    known_outcomes: list[dict[str, Any]] = []
    if saw_inquiry:
        known_outcomes.append(
            {
                "code": "member_not_found",
                "detect": {"kind": "text_visible", "text": "Record not found"},
            }
        )

    description = (goal or "Look up a member by ID and return current savings balance.").strip()
    if member_id:
        description = description.replace(str(member_id), "{member_id}")
    return {
        "apiVersion": "capability.interface.ai.local/v1",
        "id": "lookup-savings-balance",
        "name": "Lookup member savings balance",
        "version": "1",
        "description": description[:240],
        "app": {
            "surface": "web",
            "entry": "/",
            "origin_allowlist": ["http://127.0.0.1"],
        },
        "input_schema": {
            "type": "object",
            "required": ["member_id"],
            "properties": {
                "member_id": {"type": "string", "pattern": "^[0-9]+$"},
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "member_name": {"type": "string"},
                "savings_balance": {"type": "string"},
            },
        },
        "risk": "safe",
        "checkpoint": {
            "kind": "role_name_visible",
            "role": "heading",
            "name": heading_name,
        },
        "known_outcomes": known_outcomes,
        "steps": steps,
    }


def _infer_output_key(node: dict[str, Any]) -> str:
    nearby = (node.get("nearby") or "").lower()
    if "savings" in nearby or "balance" in nearby:
        return "savings_balance"
    if "name" in nearby:
        return "member_name"
    return "savings_balance"


def artifact_contains_run_id(artifact: dict[str, Any], member_id: str) -> bool:
    blob = str(artifact)
    return member_id in blob
