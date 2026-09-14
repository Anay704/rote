"""The replay result contract returned to the calling agent.

Four mutually exclusive terminal statuses carry different obligations for the caller:

  succeeded         outputs are present and the success screen was verified
  business_outcome  the app gave a legitimate answer that is not "success" (no such member, access
                    denied, validation rejected the input). Not an error; do not retry blindly.
  failed            the automation could not complete. `failure` says which step, what was
                    expected, what was observed, and whether a retry could plausibly help.
  needs_human       execution paused on an intervention (approval, unknown state, manual step).
  invalid_request   rejected before touching the UI (bad inputs, unapproved capability, policy).

Recoverable conditions never surface as a status; they appear in `recoveries` for observability.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["succeeded", "business_outcome", "failed", "needs_human", "invalid_request", "aborted"]

FailureCode = Literal[
    "LOCATOR_NOT_FOUND",      # no strategy resolved the target
    "LOCATOR_AMBIGUOUS",      # a strategy matched more than one control and nothing unique remained
    "LOCATOR_CONFLICT",       # two strategies resolved to different controls; refusing to guess
    "UNEXPECTED_STATE",       # expected screen never appeared and no known condition explains it
    "SCREEN_MISMATCH",        # a step's precondition screen did not hold
    "CHECKPOINT_FAILED",      # success criteria not met at the end
    "OUTPUT_PARSE_ERROR",     # extracted text did not parse as the declared type
    "RECOVERY_EXHAUSTED",     # a recoverable condition kept recurring
    "RECOVERY_UNSAFE",        # recovery would repeat an irreversible step
    "POLICY_VIOLATION",       # an action fell outside the allowlist at runtime
    "AUTH_FAILED",
    "HARD_CONDITION",         # a profile condition classed as hard_failure
    "RESYNC_FAILED",          # after a human handback, the current screen matched no remaining step
    "SURFACE_ERROR",          # browser/driver crashed or timed out acting
]


class Failure(BaseModel):
    code: FailureCode
    message: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: Any = None
    observed: Any = None
    retryable: bool = False
    evidence: dict[str, str] = Field(default_factory=dict)


class BusinessOutcome(BaseModel):
    code: str
    message: str
    step_id: str | None = None


class Recovery(BaseModel):
    condition: str
    action: str
    step_id: str | None
    attempt: int


class Warning_(BaseModel):
    code: Literal["LOCATOR_DEGRADED", "FRAME_FALLBACK", "DRAFT_CAPABILITY", "UNCONTROLLED_INPUT"]
    step_id: str | None = None
    target: str | None = None
    detail: str


class Escalation(BaseModel):
    intervention_id: str
    kind: str
    reason: str
    step_id: str | None = None
    resolution: dict[str, Any] | None = None


class ReplayResult(BaseModel):
    run_id: str
    capability: str
    tenant: str
    status: Status
    outputs: dict[str, Any] | None = None
    outcome: BusinessOutcome | None = None
    failure: Failure | None = None
    escalations: list[Escalation] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)
    warnings: list[Warning_] = Field(default_factory=list)
    steps_executed: int = 0
    attempts: int = 1
    duration_ms: int = 0
    evidence_dir: str
