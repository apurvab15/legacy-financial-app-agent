"""Load policies/allowlist.yaml and decide allow vs block. No HITL here (Phase 5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from src.redact import redact_text


class PolicyDenied(Exception):
    def __init__(self, outcome_code: str, *, expected: str, observed: str) -> None:
        super().__init__(outcome_code)
        self.outcome_code = outcome_code
        self.expected = expected
        self.observed = observed


@dataclass
class Policy:
    origins: list[str]
    path_prefixes: list[str]
    allowed_actions: set[str]
    denied_actions: set[str]
    irreversible_mode: str = "block"
    irreversible_names: list[str] = field(default_factory=list)
    irreversible_risks: list[str] = field(default_factory=list)

    def origin_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return True
        host = (parsed.hostname or "").lower()
        scheme = parsed.scheme
        for allowed in self.origins:
            item = urlparse(allowed)
            if (item.hostname or "").lower() != host:
                continue
            if item.scheme and item.scheme != scheme:
                continue
            if item.port is None or item.port == parsed.port:
                return True
        return False

    def path_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return True
        path = parsed.path or "/"
        for prefix in self.path_prefixes:
            if prefix == "/":
                if path == "/":
                    return True
                continue
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return True
        return False

    def url_allowed(self, url: str) -> bool:
        return self.origin_allowed(url) and self.path_allowed(url)


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    irreversible = data.get("irreversible") or {}
    return Policy(
        origins=list(data.get("origins") or []),
        path_prefixes=list(data.get("path_prefixes") or ["/"]),
        allowed_actions=set(data.get("allowed_actions") or []),
        denied_actions=set(data.get("denied_actions") or []),
        irreversible_mode=str(irreversible.get("mode") or "block"),
        irreversible_names=list(irreversible.get("control_names") or []),
        irreversible_risks=list(irreversible.get("risk_levels") or ["irreversible"]),
    )


def assert_navigation_allowed(policy: Policy, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return
    if not policy.origin_allowed(url):
        raise PolicyDenied(
            "origin_not_allowlisted",
            expected=f"origin in {policy.origins}",
            observed=redact_text(url),
        )
    if not policy.path_allowed(url):
        raise PolicyDenied(
            "path_not_allowlisted",
            expected=f"path prefix in {policy.path_prefixes}",
            observed=redact_text(parsed.path or "/"),
        )


def assert_action_allowed(policy: Policy, action: str) -> None:
    name = (action or "").strip().lower()
    if name in policy.denied_actions or name not in policy.allowed_actions:
        raise PolicyDenied(
            "action_not_allowlisted",
            expected=f"one of {sorted(policy.allowed_actions)}",
            observed=name or "(empty)",
        )


def _target_names(step: dict[str, Any]) -> list[str]:
    names: list[str] = []
    target = step.get("target") or {}
    for strategy in target.get("strategies") or []:
        for key in ("name", "text"):
            value = strategy.get(key)
            if value:
                names.append(str(value))
    return names


def is_irreversible_step(policy: Policy, action: str, step: dict[str, Any], capability: dict[str, Any]) -> bool:
    if action not in {"click", "fill"}:
        return False
    risk = (step.get("risk") or capability.get("risk") or "safe").lower()
    if risk in {r.lower() for r in policy.irreversible_risks}:
        return True
    blocked = {n.lower() for n in policy.irreversible_names}
    return any(name.lower() in blocked for name in _target_names(step))


def assert_not_irreversible(
    policy: Policy, action: str, step: dict[str, Any], capability: dict[str, Any]
) -> None:
    if not is_irreversible_step(policy, action, step, capability):
        return
    names = _target_names(step) or [str(capability.get("risk"))]
    raise PolicyDenied(
        "irreversible_blocked",
        expected="safe or reversible action (HITL not enabled in Phase 3)",
        observed=redact_text(", ".join(names)),
    )


def before_act(
    policy: Policy,
    *,
    action: str,
    step: dict[str, Any],
    capability: dict[str, Any],
    current_urls: list[str] | None = None,
) -> None:
    """Run before every fill/click/extract. Fail closed."""
    assert_action_allowed(policy, action)
    assert_not_irreversible(policy, action, step, capability)
    for url in current_urls or []:
        if url:
            assert_navigation_allowed(policy, url)
