"""Deterministic replay: execute a capability with inputs, no model in the loop.

Determinism comes from four rules:
  1. Act only on a uniquely resolved target (see WebSurface.resolve); never "the first match".
  2. Before each step, prove we are on the step's screen; after each transition, wait for the
     expected screen by polling state, not by sleeping.
  3. Every deviation is classified by the app profile into business outcome / recoverable / hard,
     and each class has exactly one response. Anything unclassified is UNEXPECTED_STATE.
  4. Recovery by restart is allowed only while no irreversible step has executed in this attempt.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from rote.control import ControlChannel, Intervention
from rote.evidence import Evidence
from rote.policy import Policy
from rote.redact import redact_page_text, redact_text, redact_value
from rote.registry import Registry, version_in_range
from rote.replay.conditions import (
    ConditionMatch,
    Probe,
    detectors_hold,
    explain_screen,
    match_condition,
    screen_holds,
    wait_for_screen,
)
from rote.schema.capability import (
    Capability,
    ClickStep,
    ExtractStep,
    FillStep,
    Literal_,
    ManualStep,
    PageScope,
    ParamRef,
    PressStep,
    SecretRef,
    SelectStep,
    Target,
)
from rote.schema.profile import AppProfile, Dismiss, Restart
from rote.schema.result import BusinessOutcome, Escalation, Failure, Recovery, ReplayResult, Warning_
from rote.schema.tenant import Tenant
from rote.surface.base import LocatorError, Surface
from rote.values import SecretProvider, parse_output, validate_inputs

PRE_SCREEN_TIMEOUT_MS = 8000

ORDER = {"safe": 0, "reversible": 1, "irreversible": 2}


# ------------------------------------------------------------------ internal control-flow signals
class _Outcome(Exception):
    def __init__(self, outcome: BusinessOutcome):
        self.outcome = outcome


class _Fail(Exception):
    def __init__(self, failure: Failure):
        self.failure = failure


class _Restart(Exception):
    def __init__(self, match: ConditionMatch, recovery: Restart, step_id: str | None):
        self.match, self.recovery, self.step_id = match, recovery, step_id


class _Unknown(Exception):
    """Expected state never arrived and nothing in the profile explains it."""

    def __init__(self, index: int, phase: str, expected: str, observed: dict):
        self.index, self.phase, self.expected, self.observed = index, phase, expected, observed


class _Halt(Exception):
    """Stop with a prepared terminal result (needs_human / aborted)."""

    def __init__(self, status: str, escalation: Escalation | None = None, failure: Failure | None = None):
        self.status, self.escalation, self.failure = status, escalation, failure


@dataclass
class ReplayOptions:
    approvals: set[str] = field(default_factory=set)
    allow_draft: bool = False
    escalation: Literal["wait", "return"] = "return"
    escalation_timeout_s: float = 900


@dataclass
class _Attempt:
    number: int
    irreversible_done: bool = False
    last_irreversible_index: int = -1


class Replayer:
    def __init__(self, *, registry: Registry, surface: Surface, profile: AppProfile, policy: Policy, tenant: Tenant,
                 evidence: Evidence, control: ControlChannel, secrets: SecretProvider,
                 options: ReplayOptions | None = None):
        self.registry = registry
        self.surface = surface
        self.profile = profile
        self.policy = policy
        self.tenant = tenant
        self.ev = evidence
        self.control = control
        self.secrets = secrets
        self.opt = options or ReplayOptions()
        self._recoveries: dict[str, int] = {}
        self._recovery_log: list[Recovery] = []
        self._attempt_no = 1
        self._cap_outputs: dict = {}

    # ================================================================== public entry point
    async def run(self, cap: Capability, inputs: dict[str, Any], applied_overlays: list[str] | None = None) -> ReplayResult:
        t0 = time.monotonic()
        result = ReplayResult(run_id=self.ev.run_id, capability=cap.ref, tenant=self.tenant.id, status="failed",
                              evidence_dir=self.ev.rel_dir)
        self.ev.event("replay.start", capability=cap.ref, tenant=self.tenant.id, app_version=self.tenant.app_version,
                      overlays=applied_overlays or [],
                      inputs={k: redact_value(v, cap.inputs[k].classification) for k, v in inputs.items()
                              if k in cap.inputs})

        self._cap_outputs = cap.outputs
        if hasattr(self.surface, "log_terms"):
            self.surface.log_terms = [str(v) for k, v in inputs.items()  # type: ignore[attr-defined]
                                      if k in cap.inputs and cap.inputs[k].classification in ("pii", "financial")]
        rejected = self._validate_request(cap, inputs, result)
        if rejected:
            return self._finish(result, t0)

        outputs: dict[str, Any] = {}
        attempt = _Attempt(1)
        start, need_entry = 0, True
        while True:
            result.attempts = self._attempt_no = attempt.number
            try:
                if need_entry:
                    await self._enter(cap)
                await self._execute(cap, inputs, outputs, start, attempt, result)
                await self._verify_success(cap, outputs)
                result.status, result.outputs = "succeeded", outputs
                shot = self.ev.screenshot(await self.surface.screenshot(), "success")
                self.ev.event("replay.checkpoint_verified", screen=cap.success.screen, screenshot=shot)
                break
            except _Outcome as o:
                result.status, result.outcome = "business_outcome", o.outcome
                shot = self.ev.screenshot(await self.surface.screenshot(), f"outcome-{o.outcome.code}")
                self.ev.event("replay.business_outcome", code=o.outcome.code, step=o.outcome.step_id, screenshot=shot)
                break
            except _Restart as r:
                if attempt.irreversible_done:
                    result.failure = await self._failure("RECOVERY_UNSAFE", r.step_id, cap,
                        f"{r.match.condition.code} after an irreversible step; restarting could repeat it",
                        expected="restartable state", observed=r.match.message, retryable=False)
                    break
                if attempt.number >= self.profile.max_attempts:
                    result.failure = await self._failure("RECOVERY_EXHAUSTED", r.step_id, cap,
                        f"{r.match.condition.code} persisted across {attempt.number} attempts",
                        expected="condition to clear", observed=r.match.message, retryable=True)
                    break
                self.ev.event("replay.restart", condition=r.match.condition.id, reauthenticate=r.recovery.reauthenticate,
                              next_attempt=attempt.number + 1, backoff_ms=r.recovery.backoff_ms)
                await asyncio.sleep(r.recovery.backoff_ms / 1000)
                attempt = _Attempt(attempt.number + 1)
                outputs.clear()
                start, need_entry = 0, True
                continue
            except _Unknown as u:
                resumed = await self._escalate_unknown(cap, u, attempt, outputs, result)
                if resumed is None:
                    break  # result already populated
                start, need_entry = resumed, False
                continue
            except _Fail as f:
                result.failure = f.failure
                break
            except _Halt as h:
                result.status = h.status  # type: ignore[assignment]
                if h.escalation:
                    result.escalations.append(h.escalation)
                result.failure = h.failure
                break
        return self._finish(result, t0)

    # ================================================================== request validation
    def _validate_request(self, cap: Capability, inputs: dict[str, Any], result: ReplayResult) -> bool:
        problems = validate_inputs(cap, inputs)
        if problems:
            result.status = "invalid_request"
            result.outcome = BusinessOutcome(code="INVALID_INPUT", message="; ".join(f"{p['input']}: {p['problem']}"
                                                                                     for p in problems))
            self.ev.event("replay.invalid_request", problems=problems)
            return True
        if cap.review.status == "approved" and cap.review.approved_digest and \
                cap.review.approved_digest != cap.procedure_digest():
            cap.review.status = "draft"  # procedure changed since approval: treat as unapproved
            result.warnings.append(Warning_(code="DRAFT_CAPABILITY", detail="procedure changed after approval"))
        if cap.review.status != "approved":
            if not self.opt.allow_draft:
                result.status = "invalid_request"
                result.outcome = BusinessOutcome(code="CAPABILITY_NOT_APPROVED",
                                                 message=f"{cap.ref} is {cap.review.status}; unattended replay needs approval")
                self.ev.event("replay.invalid_request", reason="not approved")
                return True
            result.warnings.append(Warning_(code="DRAFT_CAPABILITY", detail=f"replaying {cap.review.status} artifact"))
        if not version_in_range(self.tenant.app_version, cap.app.compatible_versions):
            result.status = "invalid_request"
            result.outcome = BusinessOutcome(code="APP_VERSION_UNSUPPORTED",
                                             message=f"tenant runs {self.tenant.app_version}; capability supports "
                                                     f"{cap.app.compatible_versions}")
            return True
        return False

    # ================================================================== session entry / auth
    async def enter_session(self) -> None:
        """Navigate to the app entry point, signing on first if the session is not authenticated."""
        await self.surface.goto(self.profile.entry_path)
        if await detectors_hold(Probe(self.surface), self.profile.auth.signed_out_screen):
            await self._authenticate()
            await self.surface.goto(self.profile.entry_path)

    async def _enter(self, cap: Capability) -> None:
        await self.enter_session()
        await self._await(cap, cap.entry_screen, 0, "entry", PRE_SCREEN_TIMEOUT_MS)

    async def _authenticate(self) -> None:
        auth = self.registry.load(self.profile.auth.capability)
        self.ev.event("auth.start", capability=auth.ref)
        for key in self.profile.auth.secrets.values():
            if not self.secrets.get(key):
                raise _Fail(Failure(code="AUTH_FAILED", message=f"secret {key} is not configured"))
        await self.surface.goto("/signon")
        try:
            await self._execute(auth, {}, {}, 0, _Attempt(0), None)
        except _Outcome as o:
            raise _Fail(Failure(code="AUTH_FAILED", message=o.outcome.message)) from None
        except _Unknown as u:
            raise _Fail(Failure(code="AUTH_FAILED", message="sign-on did not reach the main screen",
                                observed=u.observed)) from None
        self.ev.event("auth.ok")

    # ================================================================== the step loop
    async def _execute(self, cap: Capability, inputs: dict[str, Any], outputs: dict[str, Any], start: int,
                       attempt: _Attempt, result: ReplayResult | None) -> None:
        steps = cap.steps
        for i in range(start, len(steps)):
            step = steps[i]
            await self._boundary(cap, step)
            await self._await(cap, step.screen, i, "pre", PRE_SCREEN_TIMEOUT_MS)

            if isinstance(step, ManualStep):
                await self._manual(cap, step, i, result)
                continue

            target = cap.targets[step.target]
            res = await self._resolve(cap, step, target, result)
            info = await self.surface.describe(res)
            decision = self.policy.check("replay", step.action,
                                         name=info.control_name, role=info.role, is_submit=info.submit,
                                         href=info.href, recorded_risk=step.risk)
            self.ev.event("policy.decision", step=step.id, allowed=decision.allowed, risk=decision.risk,
                          reason=decision.reason)
            if not decision.allowed:
                raise _Fail(await self._failure("POLICY_VIOLATION", step.id, cap, decision.reason,
                                                expected="allowlisted action", observed=info.name))
            if decision.needs_approval and step.id not in self.opt.approvals:
                await self._approval(cap, step, i, decision.reason, result)
            epoch = self.control.epoch
            self.control.assert_automation(epoch)

            t_step = time.monotonic()
            if isinstance(step, ClickStep):
                await self.surface.click(res)
            elif isinstance(step, FillStep):
                await self.surface.fill(res, self._value(cap, step.value, inputs))
            elif isinstance(step, SelectStep):
                wanted = self._value(cap, step.value, inputs)
                try:
                    await self.surface.select(res, wanted, step.match)
                except LookupError:
                    # The live list is authoritative (options are often record-specific). A value the
                    # screen doesn't offer is the caller's answer, not a crash.
                    offered = [o for o in await self.surface.options(res) if o and not o.startswith("--")]
                    raise _Outcome(BusinessOutcome(
                        code="OPTION_UNAVAILABLE", step_id=step.id,
                        message=f"'{redact_text(wanted, 'log')}' is not offered by {step.target}; "
                                f"offered: {[redact_text(o, 'log') for o in offered]}")) from None
            elif isinstance(step, PressStep):
                await self.surface.press(res, step.key)
            elif isinstance(step, ExtractStep):
                spec = cap.outputs[step.output]
                raw = await self.surface.read_text(res)
                try:
                    outputs[step.output] = parse_output(spec, raw)
                except ValueError as e:
                    raise _Fail(await self._failure("OUTPUT_PARSE_ERROR", step.id, cap, str(e),
                                                    expected=spec.type, observed=redact_text(raw, "log"))) from None
            if decision.risk == "irreversible":
                attempt.irreversible_done = True
                attempt.last_irreversible_index = i
            if result is not None:
                result.steps_executed += 1
            self.ev.event("step.done", step=step.id, action=step.action, intent=step.intent, strategy=res.strategy,
                          risk=decision.risk, ms=round((time.monotonic() - t_step) * 1000))

            expect = getattr(step, "expect", None)
            if expect:
                await self._await(cap, expect.screen, i, "post", expect.timeout_ms)

    async def _boundary(self, cap: Capability, step) -> None:
        if self.control.preempt_requested:
            shot = self.ev.screenshot(await self.surface.screenshot(), f"preempt-{step.id}")
            iv = await self.control.yield_if_preempted({"capability": cap.ref, "step": step.id}, shot,
                                                       self.opt.escalation_timeout_s)
            if iv and iv.decision == "abort":
                raise _Halt("aborted", self._esc(iv, step.id))
        self.control.assert_automation()

    async def _resolve(self, cap: Capability, step, target: Target, result: ReplayResult | None):
        try:
            res = await self.surface.resolve(target)
        except LocatorError as e:
            # A condition may explain why the control is missing (e.g. an interstitial covers it).
            cm = await match_condition(Probe(self.surface), self.profile.conditions)
            if cm:
                await self._handle_condition(cap, cm, step.id)
                return await self._resolve(cap, step, target, result)
            raise _Fail(await self._failure(e.code, step.id, cap, str(e),  # type: ignore[arg-type]
                                            expected={"target": step.target, "locators": [
                                                loc.model_dump() for loc in target.locators]},
                                            observed={"attempts": e.attempts, "candidates": e.candidates},
                                            retryable=False)) from None
        if res.strategy_index > 0 and target.locators[res.strategy_index].strategy == "css" and \
                (isinstance(step, ExtractStep) or step.risk == "irreversible"):
            # Degrading to a structural path is acceptable for navigation, not for returning financial data or
            # committing a change: there a silently wrong match is worse than a clear failure.
            raise _Fail(await self._failure("LOCATOR_NOT_FOUND", step.id, cap,
                                            f"semantic locators failed for {step.target}; refusing structural "
                                            f"fallback for a {'data read' if isinstance(step, ExtractStep) else 'commit'}",
                                            expected={"target": step.target, "locators": [
                                                loc.model_dump() for loc in target.locators]},
                                            observed={"used": res.strategy, "notes": res.notes,
                                                      "candidates": await self._candidates(target)}))
        if result is not None and res.strategy_index > 0:
            result.warnings.append(Warning_(code="LOCATOR_DEGRADED", step_id=step.id, target=step.target,
                                            detail=f"used fallback #{res.strategy_index} ({res.strategy}); "
                                                   + "; ".join(res.notes)))
            self.ev.event("locator.degraded", step=step.id, target=step.target, used=res.strategy, notes=res.notes)
        if result is not None and res.frame_fallback:
            result.warnings.append(Warning_(code="FRAME_FALLBACK", step_id=step.id, target=step.target,
                                            detail="frame matched by url path, not name"))
        return res

    def _value(self, cap: Capability, src, inputs: dict[str, Any]) -> str:
        if isinstance(src, ParamRef):
            return str(inputs[src.param])
        if isinstance(src, SecretRef):
            key = self.profile.auth.secrets.get(src.secret, src.secret)
            v = self.secrets.get(key)
            if v is None:
                raise _Fail(Failure(code="AUTH_FAILED", message=f"secret {key} not configured"))
            return v
        assert isinstance(src, Literal_)
        return src.literal

    # ================================================================== waiting and conditions
    async def _await(self, cap: Capability, screen_id: str, index: int, phase: str, timeout_ms: int) -> None:
        step_id = cap.steps[index].id if 0 <= index < len(cap.steps) else None
        while True:
            kind, cm = await wait_for_screen(self.surface, cap, screen_id, self.profile.conditions, timeout_ms)
            if kind == "screen":
                return
            if kind == "condition" and cm:
                await self._handle_condition(cap, cm, step_id)
                continue  # dismissed; keep waiting for the same screen with a fresh deadline
            diagnosis = await explain_screen(Probe(self.surface), cap, screen_id)
            observed = {**await self._observed(), "screen_rules": diagnosis}
            if any(d["scope_missing"] for d in diagnosis):
                # A frame/window the screen lives in doesn't exist at all: that's structural drift (different
                # tenant config or app version), not a runtime state a human can click past.
                raise _Fail(await self._failure("SCREEN_MISMATCH", step_id, cap,
                                                f"screen '{screen_id}' ({phase}) cannot exist here: a scope it needs "
                                                f"is missing (likely tenant/version drift)",
                                                expected=screen_id, observed=observed))
            raise _Unknown(index, phase, screen_id, observed)

    async def _handle_condition(self, cap: Capability, cm: ConditionMatch, step_id: str | None) -> None:
        c = cm.condition
        self.ev.event("condition.detected", condition=c.id, klass=c.klass, code=c.code, step=step_id,
                      message=cm.message)
        if c.klass == "business_outcome":
            raise _Outcome(BusinessOutcome(code=c.code, message=cm.message, step_id=step_id))
        if c.klass == "hard_failure":
            raise _Fail(await self._failure("HARD_CONDITION", step_id, cap, f"{c.code}: {cm.message}",
                                            expected="no error condition", observed=cm.message))
        count = self._recoveries.get(c.id, 0) + 1
        self._recoveries[c.id] = count
        if count > c.max_recoveries:
            raise _Fail(await self._failure("RECOVERY_EXHAUSTED", step_id, cap,
                                            f"{c.code} recurred {count} times (limit {c.max_recoveries})",
                                            expected="condition to clear", observed=cm.message, retryable=True))
        rec = c.recovery
        if rec is None:
            raise _Fail(await self._failure("HARD_CONDITION", step_id, cap, f"{c.code} has no recovery",
                                            observed=cm.message))
        shot = self.ev.screenshot(await self.surface.screenshot(), f"recover-{c.id}")
        self._recovery_log.append(Recovery(condition=c.id, action=rec.kind, step_id=step_id,
                                           attempt=self._attempt_no))
        self.ev.event("recovery.apply", condition=c.id, action=rec.kind, screenshot=shot)
        if isinstance(rec, Restart):
            raise _Restart(cm, rec, step_id)
        assert isinstance(rec, Dismiss)
        target = Target(description=f"dismiss control for {c.id}", scope=rec.scope, locators=rec.locators)
        try:
            res = await self.surface.resolve(target)
        except LocatorError as e:
            raise _Fail(await self._failure(e.code, step_id, cap, f"cannot dismiss {c.code}: {e}",  # type: ignore[arg-type]
                                            observed={"attempts": e.attempts})) from None
        info = await self.surface.describe(res)
        decision = self.policy.check("replay", "dismiss", name=info.control_name, role=info.role,
                                     is_submit=info.submit, href=info.href)
        if decision.risk == "irreversible" or not decision.allowed:
            raise _Fail(await self._failure("POLICY_VIOLATION", step_id, cap,
                                            f"dismiss control '{info.name}' is not classified safe", observed=info.name))
        self.control.assert_automation()
        await self.surface.click(res)
        # Wait for the interstitial to go away so the same condition isn't counted twice.
        for _ in range(40):
            await asyncio.sleep(0.15)
            if not await match_condition(Probe(self.surface), [c]):
                break

    async def _candidates(self, target: Target) -> list[dict]:
        cand = getattr(self.surface, "_candidates", None)
        if cand is None:
            return []
        try:
            return await cand(self.surface._frame(target.scope)[0], target)  # type: ignore[attr-defined]
        except Exception:
            return []

    async def _observed(self) -> dict:
        paths = await self.surface.all_paths() if hasattr(self.surface, "all_paths") else {}
        text = await self.surface.scope_text(PageScope())
        return {"paths": paths,
                "text_excerpt": redact_page_text((text or "")[:600], self.profile.sensitive_labels, "log")}

    # ================================================================== verification
    async def _verify_success(self, cap: Capability, outputs: dict[str, Any]) -> None:
        probe = Probe(self.surface)
        if not await screen_holds(probe, cap, cap.success.screen):
            raise _Fail(await self._failure("CHECKPOINT_FAILED", None, cap, "success screen does not hold",
                                            expected=cap.success.screen, observed=await self._observed()))
        missing = [o for o in cap.success.outputs_required if o not in outputs]
        if missing:
            raise _Fail(await self._failure("CHECKPOINT_FAILED", None, cap, f"required outputs missing: {missing}",
                                            expected=cap.success.outputs_required, observed=sorted(outputs)))

    # ================================================================== human in the loop
    def _esc(self, iv: Intervention, step_id: str | None) -> Escalation:
        return Escalation(intervention_id=iv.id, kind=iv.kind, reason=iv.reason, step_id=step_id,
                          resolution={"status": iv.status, "decision": iv.decision, "operator": iv.operator,
                                      "note": iv.note, "human_actions": iv.human_actions})

    async def _open_intervention(self, cap: Capability, kind, reason: str, step_id: str | None, context: dict,
                                 allowed) -> Intervention | None:
        shot = self.ev.screenshot(await self.surface.screenshot(), f"escalate-{kind}")
        dom = self.ev.dom(await self.surface.dom_dump(), f"escalate-{kind}")
        ctx = {"capability": cap.ref, "tenant": self.tenant.id, "step": step_id, "dom": dom, **context}
        if self.opt.escalation == "return":
            iv = Intervention(id=f"iv-{int(time.time())}", session_id=self.control.session_id, kind=kind,
                              reason=reason, context=ctx, screenshot=shot, allowed_decisions=allowed)
            self.ev.write_json(f"interventions/{iv.id}.json", iv.public())
            self.ev.event("escalation.returned", intervention=iv.id, kind=kind, reason=reason)
            raise _Halt("needs_human", self._esc(iv, step_id))
        return await self.control.escalate(kind, reason, ctx, shot, self.opt.escalation_timeout_s, allowed)

    async def _approval(self, cap: Capability, step, index: int, why: str, result: ReplayResult | None) -> None:
        iv = await self._open_intervention(cap, "approval", f"step '{step.intent}' is irreversible and needs "
                                                            f"human approval", step.id,
                                           {"intent": step.intent}, ["approve", "reject"])
        assert iv is not None
        if result is not None:
            result.escalations.append(self._esc(iv, step.id))
        if iv.status == "expired":
            raise _Halt("needs_human")
        if iv.decision != "approve":
            raise _Halt("aborted")
        self.ev.event("approval.granted", step=step.id, operator=iv.operator)

    async def _manual(self, cap: Capability, step: ManualStep, index: int, result: ReplayResult | None) -> None:
        iv = await self._open_intervention(cap, "manual_step", step.instructions, step.id, {"intent": step.intent},
                                           ["resume", "abort"])
        assert iv is not None
        if result is not None:
            result.escalations.append(self._esc(iv, step.id))
        if iv.status == "expired":
            raise _Halt("needs_human")
        if iv.decision == "abort":
            raise _Halt("aborted")
        if step.expect:
            await self._await(cap, step.expect.screen, index, "post", step.expect.timeout_ms)

    async def _escalate_unknown(self, cap: Capability, u: _Unknown, attempt: _Attempt, outputs: dict[str, Any],
                                result: ReplayResult) -> int | None:
        step = cap.steps[u.index] if 0 <= u.index < len(cap.steps) else None
        step_id = step.id if step else None
        failure = await self._failure("UNEXPECTED_STATE", step_id, cap,
                                      f"expected screen '{u.expected}' ({u.phase}) did not appear and no known "
                                      f"condition matched", expected=u.expected, observed=u.observed, retryable=False)
        try:
            iv = await self._open_intervention(cap, "unknown_state", failure.message, step_id,
                                               {"expected_screen": u.expected, "phase": u.phase,
                                                "observed": u.observed}, ["resume", "completed", "abort"])
        except _Halt as h:
            result.status = "needs_human"
            result.failure = failure
            if h.escalation:
                result.escalations.append(h.escalation)
            return None
        assert iv is not None
        result.escalations.append(self._esc(iv, step_id))
        if iv.status == "expired":
            result.status, result.failure = "needs_human", failure
            return None
        if iv.decision == "abort":
            result.status = "aborted"
            return None
        # Re-synchronise: find where the live session is now, relative to the flow.
        probe = Probe(self.surface)
        if iv.decision == "completed":
            try:
                for j in range(u.index, len(cap.steps)):
                    s = cap.steps[j]
                    if isinstance(s, ExtractStep) and await screen_holds(probe, cap, s.screen):
                        res = await self.surface.resolve(cap.targets[s.target])
                        outputs[s.output] = parse_output(cap.outputs[s.output], await self.surface.read_text(res))
            except (LocatorError, ValueError) as e:
                result.failure = await self._failure("CHECKPOINT_FAILED", step_id, cap,
                                                     f"operator marked completed, but outputs could not be read: {e}")
                return None
            try:
                await self._verify_success(cap, outputs)
            except _Fail as f:
                f.failure.message = "operator marked completed, but " + f.failure.message
                result.failure = f.failure
                return None
            result.status, result.outputs = "succeeded", outputs
            self.ev.event("replay.completed_by_human", intervention=iv.id)
            return None
        # Never resume at or before an irreversible step that already ran in this attempt.
        floor = attempt.last_irreversible_index + 1
        nxt: int | None = None
        if u.phase == "post" and await screen_holds(probe, cap, u.expected):
            nxt = u.index + 1
        else:
            for j in range(max(u.index, floor), len(cap.steps)):
                if await screen_holds(Probe(self.surface), cap, cap.steps[j].screen):
                    nxt = j
                    break
        if nxt is None:
            result.failure = await self._failure("RESYNC_FAILED", step_id, cap,
                                                 "after handback the session matched no remaining step's screen",
                                                 observed=await self._observed())
            return None
        self.ev.event("replay.resync", intervention=iv.id,
                      resume_at=cap.steps[nxt].id if nxt < len(cap.steps) else "success_check")
        return nxt

    # ================================================================== results
    async def _failure(self, code, step_id: str | None, cap: Capability, message: str, *, expected: Any = None,
                       observed: Any = None, retryable: bool = False) -> Failure:
        step = next((s for s in cap.steps if s.id == step_id), None)
        label = (step_id or "run").replace("/", "_")
        ev: dict[str, str] = {}
        try:
            shot = self.ev.screenshot(await self.surface.screenshot(), f"fail-{label}")
            if shot:
                ev["screenshot"] = shot
            ev["dom"] = self.ev.dom(await self.surface.dom_dump(), f"fail-{label}")
        except Exception as e:  # pragma: no cover
            ev["evidence_error"] = str(e)
        f = Failure(code=code, message=message, step_id=step_id, step_intent=step.intent if step else None,
                    expected=expected, observed=observed, retryable=retryable, evidence=ev)
        self.ev.event("replay.failure", **f.model_dump())
        return f

    def _finish(self, result: ReplayResult, t0: float) -> ReplayResult:
        result.recoveries = list(self._recovery_log)
        result.duration_ms = round((time.monotonic() - t0) * 1000)
        persisted = result.model_dump(mode="json")
        if result.outputs:
            # outputs go back to the caller in full; the persisted copy is redacted by classification
            cap_outputs = self._cap_outputs
            persisted["outputs"] = {k: redact_value(v, cap_outputs[k].classification if k in cap_outputs else "internal")
                                    for k, v in result.outputs.items()}
        self.ev.write_json("result.json", persisted)
        self.ev.event("replay.end", status=result.status, duration_ms=result.duration_ms,
                      steps=result.steps_executed, attempts=result.attempts)
        return result
