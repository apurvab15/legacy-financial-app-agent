"""Redaction of digit runs and secrets (CONTRACT.md 6)."""

from __future__ import annotations

from src.redact import dumps_redacted, redact_json, redact_text


def test_member_id_digit_run_is_masked() -> None:
    assert redact_text("member_id=12345") == "member_id=****2345"


def test_long_account_number_keeps_only_the_last_four() -> None:
    assert redact_text("acct 1234567890123456") == "acct ****3456"


def test_short_runs_survive_so_ports_stay_readable() -> None:
    assert redact_text("http://127.0.0.1:8000/inquiry") == "http://127.0.0.1:8000/inquiry"


def test_password_assignments_are_stripped() -> None:
    assert "hunter2" not in redact_text("password=hunter2")
    assert "hunter2" not in redact_text("Passwd: hunter2")


def test_api_keys_are_stripped() -> None:
    assert "AQ.abcdefghijklmnop" not in redact_text("api_key=AQ.abcdefghijklmnop")


def test_cookie_headers_are_stripped() -> None:
    redacted = redact_text("Set-Cookie: session=abc123def456; HttpOnly")
    assert "abc123def456" not in redacted


def test_password_input_values_are_stripped_from_dom() -> None:
    html = '<input type="password" value="s3cret-value" name="pw">'
    assert "s3cret-value" not in redact_text(html)


def test_redact_json_walks_nested_structures() -> None:
    payload = {
        "outputs": {"member_id": "12345"},
        "list": ["id 987654", {"deep": "99999"}],
    }
    redacted = redact_json(payload)
    assert redacted["outputs"]["member_id"] == "****2345"
    assert redacted["list"][0] == "id ****7654"
    assert redacted["list"][1]["deep"] == "****9999"


def test_sensitive_keys_are_replaced_wholesale() -> None:
    redacted = redact_json({"password": "hunter2", "cookies": "a=b", "secret": "x"})
    assert redacted == {"password": "***", "cookies": "***", "secret": "***"}


def test_dumps_redacted_emits_json() -> None:
    assert '"****2345"' in dumps_redacted({"member_id": "12345"})


def test_empty_and_none_are_safe() -> None:
    assert redact_text("") == ""
    assert redact_json(None) is None
    assert redact_json(7) == 7
