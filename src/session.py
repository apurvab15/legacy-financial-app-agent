"""Single Playwright session path. HITL will flip controller on this same session (Phase 5)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Route, sync_playwright

from src.guardrails import Policy, assert_navigation_allowed
from src.redact import redact_text

logger = logging.getLogger(__name__)

Controller = Literal["automation", "human"]


def _prefer_installed_chromium() -> None:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not override:
        return
    root = Path(override)
    if not root.exists() or not any(root.glob("chromium*")):
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


class WebSession:
    """One browser, one page, one tracing context. Do not launch a second Playwright path later."""

    def __init__(self, *, headed: bool = False, policy: Policy | None = None) -> None:
        self.controller: Controller = "automation"
        self.paused = False
        self.headed = headed
        self.policy = policy
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self) -> Page:
        _prefer_installed_chromium()
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=not self.headed)
        # Never persist storage_state / cookies to disk.
        self.context = self.browser.new_context()
        self.context.tracing.start(screenshots=False, snapshots=True, sources=False)
        self.page = self.context.new_page()
        if self.policy is not None:
            self._install_network_guard(self.policy)
        self.controller = "automation"
        self.paused = False
        return self.page

    def _install_network_guard(self, policy: Policy) -> None:
        if self.page is None:
            return

        def handle(route: Route) -> None:
            url = route.request.url
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                route.continue_()
                return
            if policy.url_allowed(url):
                route.continue_()
                return
            logger.warning("blocked off-allowlist request %s", redact_text(url))
            route.abort("blockedbyclient")

        self.page.route("**/*", handle)

    def goto_entry(self, base_url: str, entry: str) -> None:
        if self.page is None:
            raise RuntimeError("session not started")
        if entry == "/":
            url = base_url.rstrip("/") + "/"
        else:
            url = urljoin(base_url.rstrip("/") + "/", entry.lstrip("/"))
        if self.policy is not None:
            assert_navigation_allowed(self.policy, url)
        logger.info("goto %s", redact_text(url))
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_selector('iframe[name="content"]', timeout=15000)

    def current_urls(self) -> list[str]:
        if self.page is None:
            return []
        urls = [self.page.url]
        for frame in self.page.frames:
            if frame.url:
                urls.append(frame.url)
        return urls

    def stop_tracing(self, dest: Path) -> None:
        if self.context is None:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.context.tracing.stop(path=str(dest))

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None
        self.context = None
        self.page = None
