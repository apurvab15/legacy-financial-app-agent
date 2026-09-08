from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["success", "business_outcome", "failed", "escalated"]


@dataclass
class ReplayResult:
    status: Status
    outcome_code: str
    outputs: dict[str, Any] = field(default_factory=dict)
    failed_step: str | None = None
    expected: str = ""
    observed: str = ""
    evidence_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "outcome_code": self.outcome_code,
            "outputs": self.outputs,
            "failed_step": self.failed_step,
            "expected": self.expected,
            "observed": self.observed,
            "evidence_dir": self.evidence_dir,
        }
