"""Compact AX-tree TEXT for the discovery LLM. Refs are stable for one observation only."""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page

from src.surface import MissingFrame, frame_locator

CONTENT_FRAME = "content"

_EVAL_NODES = """() => {
  const nodes = [];
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const nearbyOf = (el) => {
    const td = el.closest && el.closest("td");
    if (td && td.previousElementSibling) return norm(td.previousElementSibling.innerText);
    return "";
  };
  const push = (role, el, extra) => {
    const isBtn = el.tagName === "BUTTON" || (el.tagName === "INPUT" && /submit|button/i.test(el.type || ""));
    const name = norm(
      el.getAttribute("aria-label") ||
      el.getAttribute("title") ||
      (isBtn ? (el.getAttribute("value") || el.innerText) : "") ||
      (el.tagName && /^H[1-6]$/.test(el.tagName) ? el.innerText : "") ||
      el.innerText ||
      ""
    );
    nodes.push({
      role,
      name,
      nearby: nearbyOf(el),
      value: norm(el.value || "").slice(0, 80),
      tag: el.tagName,
      extractable: false,
      ...extra,
    });
  };
  document.querySelectorAll(
    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio])"
  ).forEach((el) => push("textbox", el));
  document.querySelectorAll("textarea").forEach((el) => push("textbox", el));
  document.querySelectorAll("button, input[type=submit], input[type=button]").forEach((el) =>
    push("button", el)
  );
  document.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((el) => push("heading", el));
  document.querySelectorAll("a[href]").forEach((el) => push("link", el));
  document.querySelectorAll("tr").forEach((tr) => {
    const tds = Array.from(tr.querySelectorAll(":scope > td"));
    if (tds.length < 2) return;
    if (tds[1].querySelector("input,button,select,textarea")) return;
    const nearby = norm(tds[0].innerText);
    const name = norm(tds[1].innerText);
    if (nearby && name && name.length < 80) {
      nodes.push({
        role: "cell",
        name,
        nearby,
        value: name,
        tag: "TD",
        extractable: true,
      });
    }
  });
  // Status and error banners ("Record not found", validation messages) carry no
  // role and sit in single-cell rows, so without this pass the model is blind to them.
  const seen = new Set(nodes.map((n) => n.name).filter(Boolean));
  const hasTextChild = (el) =>
    Array.from(el.children).some((c) => norm(c.innerText).length > 0);
  document.querySelectorAll("div, p, span, li, td, font, b, strong").forEach((el) => {
    const row = el.closest("tr");
    // Two-column rows are already emitted as label/value cells above.
    if (row && row.querySelectorAll(":scope > td").length >= 2) return;
    if (hasTextChild(el)) return;
    const name = norm(el.innerText);
    if (!name || name.length > 120 || seen.has(name)) return;
    seen.add(name);
    nodes.push({
      role: "text",
      name,
      nearby: "",
      value: "",
      tag: el.tagName,
      extractable: false,
    });
  });
  return nodes;
}"""


def observe_nodes(
    page: Page, frame_name: str = CONTENT_FRAME, *, start_index: int = 1
) -> list[dict[str, Any]]:
    frame = page.frame(name=frame_name)
    if frame is None:
        frame_locator(page, frame_name)
        frame = page.frame(name=frame_name)
    if frame is None:
        raise MissingFrame(frame_name)
    raw = frame.evaluate(_EVAL_NODES) or []
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=start_index):
        node = dict(item)
        node["ref"] = f"e{index}"
        node["frame"] = frame_name
        nodes.append(node)
    return nodes


def format_ax_text(nodes: list[dict[str, Any]], frame_name: str = CONTENT_FRAME) -> str:
    lines = [
        f"Observation of frame '{frame_name}' (accessibility TEXT only; no screenshot, no pixels).",
        "Use refs in tools. Refs are invalid after the page changes.",
        "",
    ]
    if not nodes:
        lines.append("(no interactive nodes)")
        return "\n".join(lines)
    for node in nodes:
        parts = [f"[{node['ref']}]", f"role={node.get('role')}"]
        if node.get("name"):
            parts.append(f'name="{node["name"]}"')
        if node.get("nearby"):
            parts.append(f'nearby="{node["nearby"]}"')
        if node.get("role") == "textbox":
            parts.append(f'value="{node.get("value") or ""}"')
        if node.get("extractable"):
            parts.append("extractable")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def observe_ax_text(
    page: Page, frame_name: str = CONTENT_FRAME, *, start_index: int = 1
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Refs are never reused across observations, so a stale ref fails loudly
    instead of quietly resolving to a different control on the new page."""
    nodes = observe_nodes(page, frame_name, start_index=start_index)
    text = format_ax_text(nodes, frame_name)
    refs = {str(n["ref"]): n for n in nodes}
    return text, refs


def target_from_node(node: dict[str, Any], action: str) -> dict[str, Any]:
    """Build a capability target: role+name first, nearby_text fallback. Never CSS."""
    strategies: list[dict[str, Any]] = []
    role = node.get("role") or ""
    name = node.get("name") or ""
    nearby = node.get("nearby") or ""
    if action == "extract":
        if nearby:
            strategies.append({"strategy": "nearby_text", "text": nearby})
        if name and role in {"heading", "button", "textbox"}:
            strategies.insert(0, {"strategy": "role", "role": role, "name": name})
    else:
        if role in {"textbox", "button", "heading", "link"} and name:
            strategies.append({"strategy": "role", "role": role, "name": name})
        if nearby:
            strategies.append({"strategy": "nearby_text", "text": nearby})
        if name and not any(s.get("strategy") == "role" for s in strategies):
            strategies.append({"strategy": "accessible_name", "name": name})
    if not strategies:
        if name:
            strategies.append({"strategy": "accessible_name", "name": name})
        elif nearby:
            strategies.append({"strategy": "nearby_text", "text": nearby})
    return {"frame": node.get("frame") or CONTENT_FRAME, "strategies": strategies}
