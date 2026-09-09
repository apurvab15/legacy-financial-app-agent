"""Locator priority (CONTRACT.md 2.2).

resolve_target rejects a css-first target before it ever touches the page, so
these run without a browser: page=None is enough to prove the guard order.
"""

from __future__ import annotations

import pytest

from src.surface import CssPrimaryForbidden, LocatorMiss, resolve_target

ROLE_FIRST = {
    "frame": "content",
    "strategies": [
        {"strategy": "role", "role": "textbox", "name": "Member ID"},
        {"strategy": "nearby_text", "text": "Member ID"},
    ],
}
CSS_FIRST = {
    "frame": "content",
    "strategies": [
        {"strategy": "css", "css": "#ctl00_Main_txtCIF"},
        {"strategy": "role", "role": "textbox", "name": "Member ID"},
    ],
}
CSS_AS_FALLBACK = {
    "frame": "content",
    "strategies": [
        {"strategy": "role", "role": "textbox", "name": "Member ID"},
        {"strategy": "css", "css": "#ctl00_Main_txtCIF"},
    ],
}


def test_css_first_is_refused() -> None:
    with pytest.raises(CssPrimaryForbidden):
        resolve_target(None, CSS_FIRST, "fill", timeout_ms=1)


def test_empty_strategies_is_a_locator_miss() -> None:
    with pytest.raises(LocatorMiss):
        resolve_target(None, {"frame": "content", "strategies": []}, "fill", timeout_ms=1)


def test_css_primary_is_a_locator_miss_subclass() -> None:
    """Callers that only catch LocatorMiss still fail closed."""
    assert issubclass(CssPrimaryForbidden, LocatorMiss)


def test_role_first_passes_the_ordering_guard() -> None:
    """Gets past the css check, then fails on the page rather than the policy."""
    with pytest.raises(Exception) as excinfo:
        resolve_target(None, ROLE_FIRST, "fill", timeout_ms=1)
    assert not isinstance(excinfo.value, CssPrimaryForbidden)


def test_css_is_allowed_as_a_later_fallback() -> None:
    with pytest.raises(Exception) as excinfo:
        resolve_target(None, CSS_AS_FALLBACK, "fill", timeout_ms=1)
    assert not isinstance(excinfo.value, CssPrimaryForbidden)


def test_unknown_strategy_is_rejected() -> None:
    from src.surface import _strategy_locator

    with pytest.raises(LocatorMiss):
        _strategy_locator(None, {"strategy": "xpath_magic"}, "click")
