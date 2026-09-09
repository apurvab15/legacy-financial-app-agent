"""Every capability on disk must satisfy the locked schema (CONTRACT.md 2)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

API_VERSION = "capability.interface.ai.local/v1"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schema(repo_root: Path) -> dict:
    return _load(repo_root / "schemas" / "capability.schema.json")


def test_capabilities_exist(capability_paths: list[Path]) -> None:
    assert capability_paths, "no capability files found"


def test_capabilities_validate(capability_paths: list[Path], schema: dict) -> None:
    for path in capability_paths:
        jsonschema.validate(_load(path), schema)


def test_api_version_is_locked(capability_paths: list[Path]) -> None:
    for path in capability_paths:
        assert _load(path)["apiVersion"] == API_VERSION, path.name


def test_actions_stay_in_the_mvp_set(capability_paths: list[Path]) -> None:
    allowed = {"fill", "click", "extract"}
    for path in capability_paths:
        for step in _load(path)["steps"]:
            assert step["action"] in allowed, f"{path.name}:{step['id']}"


def test_every_step_targets_a_named_frame(capability_paths: list[Path]) -> None:
    """The mock puts all forms in a child document, so frame is required."""
    for path in capability_paths:
        for step in _load(path)["steps"]:
            assert step["target"]["frame"], f"{path.name}:{step['id']}"


def test_no_capability_uses_css_as_primary_locator(capability_paths: list[Path]) -> None:
    for path in capability_paths:
        for step in _load(path)["steps"]:
            first = step["target"]["strategies"][0]
            assert first["strategy"] != "css", f"{path.name}:{step['id']} leads with css"


def test_generated_capability_parameterizes_member_id(repo_root: Path) -> None:
    """The compiler must not bake the discovery run's member id into the skill."""
    path = repo_root / "capabilities" / "lookup-savings-balance.discovered.json"
    if not path.exists():
        pytest.skip("no discovered capability committed")
    raw = path.read_text(encoding="utf-8")
    assert "12345" not in raw, "discovery member id leaked into the artifact"
    artifact = json.loads(raw)
    assert "member_id" in artifact["input_schema"]["required"]
    bindings = [
        step.get("input_binding")
        for step in artifact["steps"]
        if step.get("input_binding")
    ]
    assert {"from": "input", "key": "member_id"} in bindings


def test_no_transcript_or_secrets_in_capabilities(capability_paths: list[Path]) -> None:
    banned = ("transcript", "messages", "chain_of_thought", "screenshot", "api_key")
    for path in capability_paths:
        raw = path.read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in raw, f"{path.name} contains {token!r}"
