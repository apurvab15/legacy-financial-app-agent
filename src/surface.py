"""Web surface adapter: AX role+name locators over Playwright.

Desktop would swap this module (OS AX/UIA), not the capability JSON.
CSS is never a primary strategy.
"""

from __future__ import annotations

import logging
from typing import Any

from playwright.sync_api import FrameLocator, Locator, Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

DEFAULT_STRATEGY_TIMEOUT_MS = 2500


class LocatorMiss(Exception):
    def __init__(self, message: str, *, expected: str, observed: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.observed = observed


class CssPrimaryForbidden(LocatorMiss):
    pass


class MissingFrame(Exception):
    def __init__(self, frame_name: str) -> None:
        super().__init__(f"frame {frame_name!r} not found")
        self.frame_name = frame_name


def frame_locator(page: Page, name: str) -> FrameLocator:
    sel = f'iframe[name="{name}"]'
    try:
        page.locator(sel).wait_for(state="attached", timeout=15000)
    except PlaywrightTimeoutError as exc:
        raise MissingFrame(name) from exc
    return page.frame_locator(sel)


def _role_locator(fl: FrameLocator, strategy: dict[str, Any]) -> Locator:
    role = strategy.get("role") or ""
    name = strategy.get("name") or ""
    return fl.get_by_role(role, name=name, exact=True)


def _nearby_text_locator(fl: FrameLocator, strategy: dict[str, Any], action: str) -> Locator:
    text = strategy.get("text") or ""
    label = fl.get_by_text(text, exact=True).first
    sibling = label.locator("xpath=following-sibling::*[1]")
    if action == "fill":
        return sibling.locator("input, textarea, [contenteditable]").first
    if action == "click":
        return sibling.locator("button, input[type=submit], input[type=button], a").first
    return sibling


def _accessible_name_locator(fl: FrameLocator, strategy: dict[str, Any]) -> Locator:
    name = strategy.get("name") or strategy.get("text") or ""
    return fl.get_by_label(name, exact=True)


def _css_locator(fl: FrameLocator, strategy: dict[str, Any]) -> Locator:
    css = strategy.get("css") or ""
    return fl.locator(css)


def _strategy_locator(fl: FrameLocator, strategy: dict[str, Any], action: str) -> Locator:
    kind = strategy.get("strategy")
    if kind == "role":
        return _role_locator(fl, strategy)
    if kind == "nearby_text":
        return _nearby_text_locator(fl, strategy, action)
    if kind == "accessible_name":
        return _accessible_name_locator(fl, strategy)
    if kind == "css":
        return _css_locator(fl, strategy)
    raise LocatorMiss(
        f"unknown strategy {kind!r}",
        expected=str(kind),
        observed="unsupported strategy",
    )


def _describe(strategy: dict[str, Any]) -> str:
    kind = strategy.get("strategy")
    if kind == "role":
        return f"role={strategy.get('role')} name={strategy.get('name')!r}"
    if kind == "nearby_text":
        return f"nearby_text={strategy.get('text')!r}"
    if kind == "accessible_name":
        return f"accessible_name={strategy.get('name') or strategy.get('text')!r}"
    if kind == "css":
        return f"css={strategy.get('css')!r}"
    return repr(strategy)


def resolve_target(page: Page, target: dict[str, Any], action: str, *, timeout_ms: int) -> Locator:
    """Resolve ordered strategies in the named frame. First hit wins. No self-heal."""
    frame_name = target.get("frame") or ""
    strategies = target.get("strategies") or []
    if not strategies:
        raise LocatorMiss("no strategies", expected="strategies[]", observed="empty")
    if strategies[0].get("strategy") == "css":
        raise CssPrimaryForbidden(
            "CSS cannot be the primary locator",
            expected="strategy role (AX role+name first)",
            observed="css as first strategy",
        )

    fl = frame_locator(page, frame_name)
    attempted: list[str] = []
    for index, strategy in enumerate(strategies):
        attempted.append(_describe(strategy))
        wait = timeout_ms if index == 0 else min(DEFAULT_STRATEGY_TIMEOUT_MS, timeout_ms)
        loc = _strategy_locator(fl, strategy, action)
        try:
            loc.wait_for(state="visible", timeout=wait)
            logger.info("resolved %s via %s", action, attempted[-1])
            return loc
        except PlaywrightTimeoutError:
            logger.info("miss %s", attempted[-1])
            continue

    raise LocatorMiss(
        f"no locator hit in frame {frame_name!r}",
        expected="; ".join(attempted),
        observed=f"not visible in frame {frame_name!r}",
    )


def fill(page: Page, target: dict[str, Any], value: str, *, timeout_ms: int) -> None:
    loc = resolve_target(page, target, "fill", timeout_ms=timeout_ms)
    loc.fill(value)


def click(page: Page, target: dict[str, Any], *, timeout_ms: int) -> None:
    loc = resolve_target(page, target, "click", timeout_ms=timeout_ms)
    loc.click()


def extract(page: Page, target: dict[str, Any], *, timeout_ms: int) -> str:
    loc = resolve_target(page, target, "extract", timeout_ms=timeout_ms)
    return loc.inner_text().strip()


def detect_visible(page: Page, frame_name: str, detect: dict[str, Any], *, timeout_ms: int = 1000) -> bool:
    fl = frame_locator(page, frame_name)
    kind = detect.get("kind")
    try:
        if kind == "text_visible":
            fl.get_by_text(detect.get("text") or "", exact=False).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            return True
        if kind == "role_name_visible":
            fl.get_by_role(detect.get("role") or "", name=detect.get("name") or "", exact=True).wait_for(
                state="visible", timeout=timeout_ms
            )
            return True
    except (PlaywrightTimeoutError, MissingFrame):
        return False
    return False


def wait_for_outcome_or_checkpoint(
    page: Page,
    frame_name: str,
    known_outcomes: list[dict[str, Any]],
    checkpoint: dict[str, Any],
    *,
    timeout_ms: int,
) -> None:
    """After a click/navigation, wait until a known banner or the checkpoint appears."""
    fl = frame_locator(page, frame_name)
    loc = fl.get_by_role(
        checkpoint.get("role") or "heading",
        name=checkpoint.get("name") or "",
        exact=True,
    )
    for outcome in known_outcomes:
        detect = outcome.get("detect") or {}
        if detect.get("kind") == "text_visible":
            loc = loc.or_(fl.get_by_text(detect.get("text") or "", exact=False))
        elif detect.get("kind") == "role_name_visible":
            loc = loc.or_(
                fl.get_by_role(detect.get("role") or "", name=detect.get("name") or "", exact=True)
            )
    loc.first.wait_for(state="visible", timeout=timeout_ms)


def content_dom(page: Page, frame_name: str) -> str:
    frame = page.frame(name=frame_name)
    if frame is None:
        return ""
    return frame.content()


def content_ax(page: Page, frame_name: str) -> Any:
    frame = page.frame(name=frame_name)
    if frame is None:
        return {"error": f"missing frame {frame_name}"}
    compact: dict[str, Any] = {
        "url": frame.url,
        "title": frame.title(),
        "text": frame.inner_text("body") if frame.query_selector("body") else "",
        "headings": frame.get_by_role("heading").all_inner_texts(),
        "buttons": frame.get_by_role("button").all_inner_texts(),
        "textboxes": [
            el.get_attribute("aria-label") or el.get_attribute("name") or ""
            for el in frame.get_by_role("textbox").all()
        ],
    }
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Accessibility.enable")
        tree = cdp.send("Accessibility.getFullAXTree")
        nodes = []
        for node in (tree or {}).get("nodes") or []:
            role = (node.get("role") or {}).get("value")
            name = (node.get("name") or {}).get("value")
            if role or name:
                nodes.append({"role": role, "name": name})
        compact["ax_nodes"] = nodes
    except Exception as exc:  # noqa: BLE001 — snapshot is best-effort evidence
        logger.warning("AX snapshot failed: %s", exc)
        compact["cdp_error"] = str(exc)
    return compact
