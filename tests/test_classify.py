"""'Record not found' is a business outcome, not a failure (CONTRACT.md 3.1).

Visibility detection is stubbed so this needs no browser; the classification and
result-mapping code under test is the real thing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import classify
from src.replay import business_outcome_result

CAPABILITY = {
    "checkpoint": {"kind": "role_name_visible", "role": "heading", "name": "Member Profile"},
    "known_outcomes": [
        {"code": "member_not_found", "detect": {"kind": "text_visible", "text": "Record not found"}}
    ],
}


def _stub_detect(visible_text: str | None):
    """Pretend only visible_text is on screen."""

    def detect(page, frame_name, detect_spec, *, timeout_ms=1000):
        target = detect_spec.get("text") or detect_spec.get("name")
        return visible_text is not None and target == visible_text

    return detect


def test_record_not_found_classifies_as_member_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classify, "detect_visible", _stub_detect("Record not found"))
    known = classify.classify_known_outcome(object(), CAPABILITY)
    assert known == {"code": "member_not_found", "observed": "Record not found"}


def test_classified_banner_becomes_business_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classify, "detect_visible", _stub_detect("Record not found"))
    known = classify.classify_known_outcome(object(), CAPABILITY)
    result = business_outcome_result(known)
    assert result.status == "business_outcome"
    assert result.outcome_code == "member_not_found"
    # A business outcome is not a crash: no failed_step, and it is not 'failed'.
    assert result.failed_step is None
    assert result.status != "failed"
    assert result.outputs == {}


def test_happy_path_is_not_classified_as_an_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classify, "detect_visible", _stub_detect("Member Profile"))
    assert classify.classify_known_outcome(object(), CAPABILITY) is None


def test_checkpoint_detection_uses_the_capability_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(classify, "detect_visible", _stub_detect("Member Profile"))
    assert classify.checkpoint_met(object(), CAPABILITY) is True
    monkeypatch.setattr(classify, "detect_visible", _stub_detect(None))
    assert classify.checkpoint_met(object(), CAPABILITY) is False


def test_describe_checkpoint_is_human_readable() -> None:
    assert classify.describe_checkpoint(CAPABILITY) == "heading 'Member Profile'"


def test_lookup_capability_declares_the_not_found_outcome(repo_root: Path) -> None:
    """Guards the contract itself: 99999 must be classifiable, not a crash."""
    capability = json.loads(
        (repo_root / "capabilities" / "lookup-savings-balance.json").read_text(encoding="utf-8")
    )
    codes = {outcome["code"] for outcome in capability["known_outcomes"]}
    assert "member_not_found" in codes
