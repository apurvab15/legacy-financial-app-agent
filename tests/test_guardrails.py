"""Allowlist, action denial and irreversible modes (CONTRACT.md 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.guardrails import (
    HumanRequired,
    PolicyDenied,
    assert_action_allowed,
    assert_navigation_allowed,
    before_act,
    is_irreversible_step,
    load_policy,
)

CONFIRM_STEP = {
    "id": "s1",
    "action": "click",
    "target": {
        "frame": "content",
        "strategies": [{"strategy": "role", "role": "button", "name": "Confirm open"}],
    },
}
SAFE_STEP = {
    "id": "s1",
    "action": "fill",
    "target": {
        "frame": "content",
        "strategies": [{"strategy": "role", "role": "textbox", "name": "Member ID"}],
    },
}


@pytest.fixture(scope="module")
def policy(repo_root: Path):
    return load_policy(repo_root / "policies" / "allowlist.yaml")


@pytest.fixture(scope="module")
def policy_hitl(repo_root: Path):
    return load_policy(repo_root / "policies" / "allowlist-hitl.yaml")


# --- origins ---------------------------------------------------------------

def test_local_mock_origin_is_allowed(policy) -> None:
    assert_navigation_allowed(policy, "http://127.0.0.1:8000/inquiry")
    assert_navigation_allowed(policy, "http://localhost:8000/")


def test_off_origin_navigation_is_denied(policy) -> None:
    with pytest.raises(PolicyDenied) as excinfo:
        assert_navigation_allowed(policy, "http://example.com/")
    assert excinfo.value.outcome_code == "origin_not_allowlisted"


def test_https_public_host_is_denied(policy) -> None:
    with pytest.raises(PolicyDenied):
        assert_navigation_allowed(policy, "https://bank.example.com/transfer")


def test_off_path_is_denied(policy) -> None:
    with pytest.raises(PolicyDenied) as excinfo:
        assert_navigation_allowed(policy, "http://127.0.0.1:8000/admin")
    assert excinfo.value.outcome_code == "path_not_allowlisted"


def test_allowed_path_prefixes(policy) -> None:
    for url in (
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8000/inquiry",
        "http://127.0.0.1:8000/member/999",
        "http://127.0.0.1:8000/sub-account/new",
    ):
        assert_navigation_allowed(policy, url)


# --- actions ---------------------------------------------------------------

def test_allowed_actions_pass(policy) -> None:
    for action in ("click", "fill", "extract", "wait"):
        assert_action_allowed(policy, action)


def test_download_is_denied(policy) -> None:
    with pytest.raises(PolicyDenied) as excinfo:
        assert_action_allowed(policy, "download")
    assert excinfo.value.outcome_code == "action_not_allowlisted"


def test_file_upload_is_denied(policy) -> None:
    with pytest.raises(PolicyDenied):
        assert_action_allowed(policy, "file_upload")


def test_unknown_action_is_denied_by_default(policy) -> None:
    """Fail closed: anything not on the allowlist is refused."""
    with pytest.raises(PolicyDenied):
        assert_action_allowed(policy, "execute_script")
    with pytest.raises(PolicyDenied):
        assert_action_allowed(policy, "")


# --- irreversible ----------------------------------------------------------

def test_confirm_open_is_detected_as_irreversible(policy) -> None:
    assert is_irreversible_step(policy, "click", CONFIRM_STEP, {"risk": "irreversible"})


def test_safe_step_is_not_irreversible(policy) -> None:
    assert not is_irreversible_step(policy, "fill", SAFE_STEP, {"risk": "safe"})


def test_mode_block_refuses_the_step(policy) -> None:
    assert policy.irreversible_mode == "block"
    with pytest.raises(PolicyDenied) as excinfo:
        before_act(policy, action="click", step=CONFIRM_STEP, capability={"risk": "irreversible"})
    assert excinfo.value.outcome_code == "irreversible_blocked"


def test_mode_require_human_asks_for_a_human(policy_hitl) -> None:
    assert policy_hitl.irreversible_mode == "require_human"
    with pytest.raises(HumanRequired) as excinfo:
        before_act(
            policy_hitl, action="click", step=CONFIRM_STEP, capability={"risk": "irreversible"}
        )
    assert excinfo.value.outcome_code == "irreversible_blocked"


def test_preflight_defers_require_human(policy_hitl) -> None:
    """A handoff needs a live session, so preflight must not raise it early."""
    before_act(
        policy_hitl,
        action="click",
        step=CONFIRM_STEP,
        capability={"risk": "irreversible"},
        preflight=True,
    )


def test_preflight_still_hard_blocks_under_mode_block(policy) -> None:
    with pytest.raises(PolicyDenied):
        before_act(
            policy,
            action="click",
            step=CONFIRM_STEP,
            capability={"risk": "irreversible"},
            preflight=True,
        )


def test_before_act_checks_current_urls(policy) -> None:
    with pytest.raises(PolicyDenied):
        before_act(
            policy,
            action="fill",
            step=SAFE_STEP,
            capability={"risk": "safe"},
            current_urls=["http://evil.example.com/"],
        )
