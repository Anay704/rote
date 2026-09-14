"""Control transfer for a live session: who may drive it, and how control moves.

State machine (one per live session):

    AUTOMATION --escalate()--> AWAITING_HUMAN --claim(op)--> HUMAN --release(decision)--> AUTOMATION
         ^                           |                                                       |
         |                           +--timeout/cancel--> (run ends as needs_human)          |
         +------------------------------------------------------------------------------------+
    AUTOMATION --preempt(op)--> HUMAN   (an operator can take over a running session; automation
                                         yields at its next action boundary)

Invariants:
  * Exactly one holder. Every automation action calls `assert_automation()`; every operator input
    carries the lease token returned by `claim()` and calls `assert_human(token)`.
  * `epoch` increments on every transfer. Automation captures the epoch before resolving a target
    and re-checks it before acting, so a preemption between "decide" and "act" is never overridden.
  * Human input is recorded twice: the raw operator-console input (click x,y / keys) and the
    semantic DOM event it produced (role/name of the control). Values typed by the human are stored
    as lengths only.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from rote.evidence import Evidence, now_iso

Decision = Literal["resume", "completed", "abort", "approve", "reject"]
Kind = Literal["stuck", "unknown_state", "approval", "manual_step", "operator_preempt", "policy_block"]


class Holder(StrEnum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"


class ControlViolation(RuntimeError):
    pass


@dataclass
class Intervention:
    id: str
    session_id: str
    kind: Kind
    reason: str
    context: dict[str, Any]
    screenshot: str | None = None  # evidence-relative path of the redacted screenshot at escalation
    created_at: str = field(default_factory=now_iso)
    status: Literal["open", "claimed", "resolved", "expired"] = "open"
    operator: str | None = None
    decision: Decision | None = None
    note: str | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    allowed_decisions: list[Decision] = field(default_factory=lambda: ["resume", "completed", "abort"])

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class ControlChannel:
    def __init__(self, session_id: str, evidence: Evidence):
        self.session_id = session_id
        self.evidence = evidence
        self.holder = Holder.AUTOMATION
        self.epoch = 0
        self._token: str | None = None
        self.current: Intervention | None = None
        self.history: list[Intervention] = []
        self._released = asyncio.Event()
        self._preempt: str | None = None

    # ---------------------------------------------------------------- automation side
    def assert_automation(self, epoch: int | None = None) -> None:
        if self.holder is not Holder.AUTOMATION:
            raise ControlViolation(f"automation attempted to act while holder={self.holder.value}")
        if epoch is not None and epoch != self.epoch:
            raise ControlViolation("control changed hands between decide and act")

    @property
    def preempt_requested(self) -> bool:
        return self._preempt is not None

    async def escalate(self, kind: Kind, reason: str, context: dict[str, Any],
                       screenshot: str | None, timeout_s: float | None,
                       allowed: list[Decision] | None = None) -> Intervention:
        """Pause automation and wait for a human to claim, act, and release. Returns the resolved
        intervention (status resolved or expired)."""
        self.assert_automation()
        iv = Intervention(id=f"iv-{uuid.uuid4().hex[:8]}", session_id=self.session_id, kind=kind, reason=reason,
                          context=context, screenshot=screenshot)
        if allowed:
            iv.allowed_decisions = allowed
        self.current = iv
        self.history.append(iv)
        self._transition(Holder.AWAITING_HUMAN, "escalated", intervention=iv.id, reason=reason, kind=kind)
        self.evidence.write_json(f"interventions/{iv.id}.json", iv.public())
        self._released.clear()
        try:
            if timeout_s is None:
                await self._released.wait()
            else:
                await asyncio.wait_for(self._released.wait(), timeout_s)
        except TimeoutError:
            if self.holder is Holder.HUMAN:
                # The timeout bounds how long we wait for *someone to pick it up*. Never yank control
                # back from an operator who is mid-task.
                await self._released.wait()
            else:
                iv.status = "expired"
                self._transition(Holder.AUTOMATION, "escalation_expired", intervention=iv.id)
        self.evidence.write_json(f"interventions/{iv.id}.json", iv.public())
        self.current = None
        return iv

    async def yield_if_preempted(self, context: dict[str, Any], screenshot: str | None,
                                 timeout_s: float | None) -> Intervention | None:
        """Called by automation at action boundaries. If an operator asked for control, open a
        preempt intervention for them to claim, and block until they release it."""
        if not self._preempt:
            return None
        op = self._preempt
        self._preempt = None
        return await self.escalate("operator_preempt", f"operator {op} requested control", context, screenshot,
                                   timeout_s)

    # ---------------------------------------------------------------- operator side
    def request_preempt(self, operator: str) -> None:
        if self.holder is not Holder.AUTOMATION:
            raise ControlViolation(f"cannot preempt: holder={self.holder.value}")
        self._preempt = operator
        self.evidence.event("control.preempt_requested", operator=operator)

    def claim(self, intervention_id: str, operator: str) -> str:
        iv = self.current
        if iv is None or iv.id != intervention_id:
            raise ControlViolation("no such open intervention")
        if self.holder is not Holder.AWAITING_HUMAN:
            raise ControlViolation(f"cannot claim: holder={self.holder.value}")
        iv.status, iv.operator = "claimed", operator
        self._token = secrets.token_urlsafe(16)
        self._transition(Holder.HUMAN, "claimed", intervention=iv.id, operator=operator)
        return self._token

    def assert_human(self, token: str) -> Intervention:
        if self.holder is not Holder.HUMAN or not self._token or not secrets.compare_digest(token, self._token):
            raise ControlViolation("operator does not hold control")
        assert self.current is not None
        return self.current

    def record_human(self, action: dict[str, Any]) -> None:
        if self.current is not None and self.holder is Holder.HUMAN:
            entry = {"at": now_iso(), **action}
            self.current.human_actions.append(entry)
            self.evidence.event("human.action", intervention=self.current.id, **action)

    def release(self, token: str, decision: Decision, note: str | None = None) -> Intervention:
        iv = self.assert_human(token)
        if decision not in iv.allowed_decisions:
            raise ControlViolation(f"decision {decision!r} not allowed here; allowed: {iv.allowed_decisions}")
        iv.status, iv.decision, iv.note = "resolved", decision, note
        self._token = None
        self._transition(Holder.AUTOMATION, "released", intervention=iv.id, decision=decision, note=note,
                         human_actions=len(iv.human_actions))
        self._released.set()
        return iv

    # ---------------------------------------------------------------- internals
    def _transition(self, to: Holder, why: str, **data: Any) -> None:
        frm = self.holder
        self.holder = to
        self.epoch += 1
        self.evidence.event("control.transfer", frm=frm.value, to=to.value, why=why, epoch=self.epoch, **data)

    def status(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "holder": self.holder.value, "epoch": self.epoch,
                "intervention": self.current.public() if self.current else None}
