"""Mask digit runs and secrets in logs and evidence. Never persist passwords or cookies."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

# Member IDs / account-like runs. 4-digit ports (8000) stay readable in URLs.
_DIGIT_RUN = re.compile(r"\d{5,}")
_COOKIE_HEADER = re.compile(r"(?i)(cookie|set-cookie)\s*[:=]\s*.+")
_PASSWORD = re.compile(
    r"(?i)(password|passwd|pwd|secret|api[_-]?key)\s*[:=]\s*\S+"
)
_PASSWORD_INPUT = re.compile(
    r'(<input\b[^>]*\btype=["\']password["\'][^>]*\bvalue=["\'])[^"\']*',
    re.IGNORECASE,
)


def mask_digit_run(match: re.Match[str]) -> str:
    digits = match.group(0)
    return "****" + digits[-4:]


def redact_text(value: str) -> str:
    if not value:
        return value
    text = _COOKIE_HEADER.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + "=***", value)
    text = _PASSWORD.sub(lambda m: re.split(r"[:=]", m.group(0), maxsplit=1)[0] + "=***", text)
    text = _PASSWORD_INPUT.sub(r"\1***", text)
    return _DIGIT_RUN.sub(mask_digit_run, text)


def redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"cookie", "cookies", "set-cookie", "password", "passwd", "secret"}:
                out[key] = "***"
            else:
                out[key] = redact_json(item)
        return out
    return value


def dumps_redacted(value: Any) -> str:
    return json.dumps(redact_json(value), indent=2)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_text(str(v)) for k, v in record.args.items()}
            else:
                record.args = tuple(
                    redact_text(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        return True
