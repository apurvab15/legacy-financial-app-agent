"""Text-only LLM client for discovery.

Provider is Google Gemini through its OpenAI-compatible chat/completions endpoint.
Text in, tool calls out: no images, no vision, no computer-use tooling.

Gemini rejects OpenAI-only request fields, so the payload stays limited to
model / messages / tools / tool_choice / temperature / max_tokens.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-2.5-flash"

# Free-tier capacity and daily quota are both per model, so when one flash model
# is saturated or spent, the next one in the chain is worth trying.
DEFAULT_FALLBACK_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-3.1-flash-lite",
)

# Gemini returns 429/503 on capacity spikes; a 20-step loop should ride those out.
# Few attempts per model on purpose: when one model is saturated, moving to the
# next is usually faster than waiting out its backoff.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_BACKOFF_S = 30.0

# Flat OpenAPI-subset schemas: object of string properties only. No nesting,
# no anyOf/oneOf, no additionalProperties — Gemini 400s on richer JSON Schema.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "fill",
            "description": "Type text into the control with this ref (usually a textbox).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Ref from the observation, e.g. e1"},
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["ref", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the control with this ref (button or link).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Ref from the observation, e.g. e2"},
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract",
            "description": "Read a labeled value from an extractable node and store it as an output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Ref of an extractable node"},
                    "key": {"type": "string", "description": "Output name, e.g. savings_balance"},
                },
                "required": ["ref", "key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Goal is complete: checkpoint is visible and outputs are extracted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One line on what was accomplished"},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Stop and hand the same session to a human. Use instead of guessing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why automation cannot continue"},
                },
                "required": ["reason"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are discovering a reusable UI skill on a local legacy banking mock.

You observe a compact accessibility tree as TEXT. There are no screenshots and you must never ask for one.
Act only in frame "content", only through the tools: fill, click, extract, done, escalate.

Rules:
- Address controls by the refs in the observation. Never use CSS selectors or generated ids like ctl00_Main_txtCIF.
- Never click "Confirm open" or otherwise open a sub-account. That action is irreversible and is blocked.
- Type the member id into the Member ID textbox, then click Inquiry.
- When the heading "Member Profile" is visible, extract the savings balance with key savings_balance, then call done.
- If the page shows "Record not found", call done and do not extract.
- If you are stuck or a needed control is missing, call escalate. Never invent a control that is not listed.
- Call exactly one tool per turn. Do not narrate at length.
"""


@dataclass
class LlmConfig:
    model: str
    api_key: str
    base_url: str
    fallback_models: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmTurn:
    assistant_message: dict[str, Any]
    tool_calls: list[ToolCall]
    model: str


class LlmError(RuntimeError):
    pass


class _ModelUnusable(RuntimeError):
    """This model cannot serve the turn; try the next one in the chain."""


def load_llm_config() -> LlmConfig:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise LlmError(
            "No GEMINI_API_KEY. Put it in .env for discovery. Replay does not need a key."
        )
    model = (os.environ.get("DISCOVERY_MODEL") or "").strip() or DEFAULT_MODEL
    base_url = (os.environ.get("DISCOVERY_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    raw_fallbacks = (os.environ.get("DISCOVERY_FALLBACK_MODELS") or "").strip()
    fallbacks = (
        [item.strip() for item in raw_fallbacks.split(",") if item.strip()]
        if raw_fallbacks
        else list(DEFAULT_FALLBACK_MODELS)
    )
    return LlmConfig(
        model=model, api_key=api_key, base_url=base_url, fallback_models=fallbacks
    )


def _model_chain(config: LlmConfig) -> list[str]:
    chain: list[str] = []
    for model in [config.model, *config.fallback_models]:
        if model and model not in chain:
            chain.append(model)
    return chain


def _endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _is_daily_quota(body: str) -> bool:
    """A per-day free-tier 429 will not clear on a retry, unlike a per-minute one.

    Google still sends a short retryDelay for it, so the body has to be read.
    """
    return "PerDay" in body or "RequestsPerDay" in body


def _retry_after(body: str) -> float | None:
    """Google's own retryDelay, e.g. "21.235186269s", beats a guessed backoff."""
    match = re.search(r'"retryDelay"\s*:\s*"([0-9.]+)s"', body)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _sanitize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini wants a string content on every message, including tool-call turns."""
    out: list[dict[str, Any]] = []
    for message in messages:
        clean = {key: value for key, value in message.items() if value is not None}
        clean.setdefault("content", "")
        out.append(clean)
    return out


def complete_turn(messages: list[dict[str, Any]], config: LlmConfig | None = None) -> LlmTurn:
    """Try each model in the chain. Free-tier capacity and daily quota are both
    per model, so a 503 or an exhausted quota on one says nothing about the next."""
    config = config or load_llm_config()
    body = _sanitize(messages)
    failures: list[str] = []
    chain = _model_chain(config)
    for index, model in enumerate(chain):
        try:
            return _turn_for_model(body, model, config)
        except _ModelUnusable as exc:
            failures.append(f"{model}: {exc}")
            remaining = chain[index + 1 :]
            if remaining:
                logger.warning("model %s unusable (%s); falling back to %s", model, exc, remaining[0])
    raise LlmError("No usable model. " + " | ".join(failures))


def _turn_for_model(body: list[dict[str, Any]], model: str, config: LlmConfig) -> LlmTurn:
    payload = {
        "model": model,
        "messages": body,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": 800,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    endpoint = _endpoint(config.base_url)
    last_error = ""
    response = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=90.0) as client:
                response = client.post(endpoint, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            last_error = f"transport error: {exc}"
            response = None
        else:
            if response.status_code < 400:
                break
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code == 429 and _is_daily_quota(response.text):
                raise _ModelUnusable("daily free-tier quota exhausted")
            if response.status_code == 404:
                raise _ModelUnusable("model not available to this key")
            if response.status_code not in RETRY_STATUS:
                # 400/401/403 mean our request or key is wrong; another model will not help.
                raise LlmError(f"LLM {last_error}")
        if attempt < MAX_ATTEMPTS:
            suggested = _retry_after(response.text) if response is not None else None
            delay = min(max(suggested or 2.0 * 2 ** (attempt - 1), 1.0), MAX_BACKOFF_S)
            delay += random.uniform(0, 0.5 * delay)
            logger.warning(
                "%s retry %s/%s in %.0fs (%s)",
                model,
                attempt,
                MAX_ATTEMPTS,
                delay,
                last_error[:110],
            )
            time.sleep(delay)
    else:
        raise _ModelUnusable(f"unavailable after {MAX_ATTEMPTS} attempts: {last_error[:200]}")

    if response is None:
        raise _ModelUnusable(f"unavailable: {last_error[:200]}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise LlmError(f"LLM returned no choices: {json.dumps(data)[:300]}")
    message = dict(choices[0].get("message") or {})
    message.setdefault("role", "assistant")
    if message.get("content") is None:
        message["content"] = ""

    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                parsed = {}
        else:
            parsed = arguments or {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=parsed,
            )
        )
    return LlmTurn(assistant_message=message, tool_calls=calls, model=model)
