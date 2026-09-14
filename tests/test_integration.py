"""Integration: real CoreServ (both tenants) + real headless Chromium + the committed artifacts.

Covers the replay contract end to end: success, each business outcome, each recovery, hard failures,
approval gating, cross-tenant overlays and drift, the human handoff, locator semantics on hostile
markup, network allowlisting, and a leak canary over every evidence file the suite produced.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

import pytest

from rote.cli import replay_once
from rote.control import Holder
from rote.replay.engine import Replayer, ReplayOptions
from rote.runtime import open_session
from rote.schema.capability import (
    LabelLocator,
    PageScope,
    RoleLocator,
    TableCellLocator,
    Target,
)
from rote.surface.base import LocatorError

BAL = "keystone.coreserv.member_savings_balance"
REVIEW = "keystone.coreserv.open_sub_account_review"
COMMIT = "keystone.coreserv.open_sub_account"
SUB = {"member_number": "48213", "product": "SHARE CERTIFICATE 12 MO", "nickname": "COLLEGE",
       "opening_deposit": "500.00", "fund_from_suffix": "0000"}


@pytest.fixture
def run(registry, runs_dir):
    async def _run(cap, inputs, tenant="prairie", faults=None, **kw):
        return await replay_once(cap, tenant, inputs, inject=faults if faults is not None else {}, echo=False,
                                 runs_dir=runs_dir, registry=registry, **kw)
    return _run


# ----------------------------------------------------------------------------- outcomes
async def test_success_returns_typed_output(run):
    r = await run(BAL, {"member_number": "12345"})
    assert r.status == "succeeded" and r.outputs == {"regular_shares_balance": "2418.37"}
    assert not r.warnings and r.attempts == 1


@pytest.mark.parametrize("member,code", [("99999", "RECORD_NOT_FOUND"), ("31008", "PERMISSION_DENIED")])
async def test_business_outcomes_are_not_failures(run, registry, member, code):
    r = await run(BAL, {"member_number": member})
    assert r.status == "business_outcome" and r.outcome.code == code and r.failure is None
    search_step = next(s for s in registry.load(BAL).steps if getattr(s, "expect", None)
                       and s.expect.screen == registry.load(BAL).success.screen)
    assert r.outcome.step_id == search_step.id  # attributed to the step whose submission produced it


async def test_bad_input_rejected_before_touching_ui(run):
    r = await run(BAL, {"member_number": "12A45"})
    assert r.status == "invalid_request" and r.outcome.code == "INVALID_INPUT" and r.steps_executed == 0


async def test_app_side_validation_is_a_business_outcome(run):
    r = await run(REVIEW, {**SUB, "opening_deposit": "99999.00"})
    assert r.status == "business_outcome" and r.outcome.code == "VALIDATION_REJECTED"


async def test_record_specific_option_unavailable(run):
    r = await run(REVIEW, {**SUB, "fund_from_suffix": "0050"})
    assert r.status == "business_outcome" and r.outcome.code == "OPTION_UNAVAILABLE"


# ----------------------------------------------------------------------------- recoverable conditions
async def test_interstitial_dismissed(run):
    r = await run(BAL, {"member_number": "20417"})
    assert r.status == "succeeded" and [x.condition for x in r.recoveries] == ["member_alert"]


async def test_transient_error_recovered_by_restart(run):
    r = await run(BAL, {"member_number": "12345"}, faults={"error_next": 1, "error_path": "/mi/detail"})
    assert r.status == "succeeded" and r.attempts == 2
    assert r.recoveries[0].condition == "transient_system_error"


async def test_persistent_error_exhausts(run):
    r = await run(BAL, {"member_number": "12345"}, faults={"error_next": 9, "error_path": "/mi/detail"})
    assert r.status == "failed" and r.failure.code == "RECOVERY_EXHAUSTED" and r.failure.retryable
    assert r.failure.evidence.get("screenshot") and r.failure.evidence.get("dom")


async def test_session_expiry_reauthenticates(run):
    r = await run(BAL, {"member_number": "12345"}, faults={"expire_session": True, "expire_path": "/mi/detail"})
    assert r.status == "succeeded" and r.recoveries[0].condition == "session_expired"


async def test_slow_load_waits_on_state_not_time(run):
    r = await run(BAL, {"member_number": "12345"},
                  faults={"latency_next": 1, "latency_ms": 2500, "latency_path": "/mi/detail"})
    assert r.status == "succeeded" and r.duration_ms >= 2500


async def test_restart_after_irreversible_step_is_refused(run):
    r = await run(COMMIT, SUB, faults={"error_next": 1, "error_path": "/sa/commit"},
                  approvals={"s10_confirm_and_open"})
    assert r.status == "failed" and r.failure.code == "RECOVERY_UNSAFE"


# ----------------------------------------------------------------------------- approval and review gates
async def test_irreversible_step_pauses_for_approval(run, registry):
    r = await run(COMMIT, SUB)
    assert r.status == "needs_human" and r.escalations[0].kind == "approval"
    assert r.steps_executed == len(registry.load(REVIEW).steps)  # stopped right before the commit


async def test_approved_commit_executes(run):
    r = await run(COMMIT, SUB, approvals={"s10_confirm_and_open"})
    assert r.status == "succeeded" and "OPENED" in r.outputs["confirmation"]


async def test_unapproved_capability_refused(run, registry):
    cap = registry.load(BAL).model_copy(deep=True)
    cap.version = "1.0.1"
    cap.review.status = "draft"
    registry.save(cap)
    try:
        r = await run(f"{BAL}@1.0.1", {"member_number": "12345"})
        assert r.status == "invalid_request" and r.outcome.code == "CAPABILITY_NOT_APPROVED"
    finally:
        registry.capability_path(BAL, "1.0.1").unlink()


# ----------------------------------------------------------------------------- tenants and drift
async def test_overlay_reuses_base_capability_on_second_tenant(run):
    r = await run(BAL, {"member_number": "12345"}, tenant="lakeshore")
    assert r.status == "succeeded" and r.outputs == {"regular_shares_balance": "2418.37"} and not r.warnings


async def test_drift_without_overlay_is_diagnosed(run, registry):
    tenant = registry.tenant("lakeshore")
    bare = tenant.model_copy(update={"overlays": []})
    p = registry.root / "tenants" / "lakeshore.yaml"
    original = p.read_text()
    import yaml
    p.write_text(yaml.safe_dump(bare.model_dump(mode="json")))
    try:
        r = await run(BAL, {"member_number": "12345"}, tenant="lakeshore")
    finally:
        p.write_text(original)
    assert r.status == "failed" and r.failure.code == "SCREEN_MISMATCH"
    assert any(rule["scope_missing"] for rule in r.failure.observed["screen_rules"])


async def test_structural_fallback_refused_for_data_reads(run, registry):
    """Frame overlay only: navigation degrades to CSS with warnings; the balance read refuses CSS."""
    tenant = registry.tenant("lakeshore")
    ov = tenant.overlays[0].model_copy(deep=True)
    ov.patch = {"screens": ov.patch["screens"], "targets": {"member_inquiry_link": ov.patch["targets"]["member_inquiry_link"]}}
    p = registry.root / "tenants" / "lakeshore.yaml"
    original = p.read_text()
    import yaml
    p.write_text(yaml.safe_dump(tenant.model_copy(update={"overlays": [ov]}).model_dump(mode="json")))
    try:
        r = await run(BAL, {"member_number": "12345"}, tenant="lakeshore")
    finally:
        p.write_text(original)
    assert {w.code for w in r.warnings} == {"LOCATOR_DEGRADED"}
    assert r.status == "failed" and r.failure.code == "LOCATOR_NOT_FOUND" and r.outputs is None
    assert "refusing structural fallback" in r.failure.message


# ----------------------------------------------------------------------------- human handoff
async def test_unknown_state_handoff_resume_and_resync(registry, runs_dir, apps):
    _faults(apps["prairie"], {"unknown_modal_next": 1, "unknown_modal_path": "/mi/detail"})
    async with open_session("replay", "prairie", registry=registry, runs_dir=runs_dir, echo=False) as s:
        rep = Replayer(registry=registry, surface=s.surface, profile=s.profile, policy=s.policy, tenant=s.tenant,
                       evidence=s.evidence, control=s.control, secrets=s.secrets,
                       options=ReplayOptions(escalation="wait", escalation_timeout_s=30))

        async def operator():
            while s.control.current is None:
                await asyncio.sleep(0.05)
            iv = s.control.current
            token = s.control.claim(iv.id, "test.operator")
            dismiss = await s.surface.resolve(Target(description="d", scope=_content(),
                                                     locators=[RoleLocator(role="button", name="DISMISS")]))
            await s.surface.click(dismiss)  # stands in for the operator's click in the live session
            await asyncio.sleep(0.5)
            s.control.release(token, "resume", "dismissed printer prompt")

        op = asyncio.create_task(operator())
        result = await rep.run(registry.load(BAL), {"member_number": "12345"})
        await op
    assert result.status == "succeeded" and result.outputs == {"regular_shares_balance": "2418.37"}
    esc = result.escalations[0]
    assert esc.kind == "unknown_state" and esc.resolution["decision"] == "resume"
    assert s.control.holder is Holder.AUTOMATION
    events = [json.loads(line)["event"] for line in (s.evidence.dir / "events.jsonl").read_text().splitlines()]
    assert "replay.resync" in events and events.count("control.transfer") == 3


async def test_escalation_returns_immediately_for_unattended_callers(run):
    r = await run(BAL, {"member_number": "12345"}, faults={"unknown_modal_next": 1, "unknown_modal_path": "/mi/detail"})
    assert r.status == "needs_human" and r.failure.code == "UNEXPECTED_STATE"
    assert r.escalations and r.escalations[0].kind == "unknown_state"


# ----------------------------------------------------------------------------- surface semantics and guardrails
def _content():
    from rote.schema.capability import FrameScope
    return FrameScope(name="content")


def _faults(base: str, body: dict) -> None:
    for path, b in (("/__reset", {}), ("/__faults", body)):
        req = urllib.request.Request(base + path, data=json.dumps(b).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        urllib.request.urlopen(req).read()


async def test_locator_ambiguity_and_conflict(registry, runs_dir):
    async with open_session("unit", "prairie", registry=registry, runs_dir=runs_dir, echo=False) as s:
        await s.surface.page.set_content("""
          <table><tr><td>Amount:</td><td><input name=a></td></tr>
                 <tr><td>Amount:</td><td><input name=b></td></tr>
                 <tr><td>Other:</td><td><input name=c aria-label="Total"></td></tr></table>
          <table><tr><td>ACCT</td><td>BAL</td></tr><tr><td>SAVINGS</td><td>$1.00</td></tr></table>""")
        with pytest.raises(LocatorError) as amb:
            await s.surface.resolve(Target(description="x", scope=PageScope(),
                                           locators=[LabelLocator(role="textbox", label="Amount:")]))
        assert amb.value.code == "LOCATOR_AMBIGUOUS"
    async with open_session("unit", "prairie", registry=registry, runs_dir=runs_dir, echo=False) as s:
        await s.surface.page.set_content("""
          <table><tr><td>Other:</td><td><input name=c></td></tr>
                 <tr><td>Amount:</td><td><input name=d aria-label="Total"></td></tr></table>
          <table><tr><td>ACCT</td><td>BAL</td></tr><tr><td>SAVINGS</td><td>$1.00</td></tr></table>""")
        with pytest.raises(LocatorError) as conflict:
            await s.surface.resolve(Target(description="x", scope=PageScope(), locators=[
                RoleLocator(role="textbox", name="Total"), LabelLocator(role="textbox", label="Other:")]))
        assert conflict.value.code == "LOCATOR_CONFLICT"
        cell = await s.surface.resolve(Target(description="x", scope=PageScope(),
                                              locators=[TableCellLocator(row_key="SAVINGS", column="BAL")]))
        assert await s.surface.read_text(cell) == "$1.00"


async def test_network_allowlist_blocks_page_initiated_navigation(registry, runs_dir, apps):
    async with open_session("unit", "prairie", registry=registry, runs_dir=runs_dir, echo=False) as s:
        await Replayer(registry=registry, surface=s.surface, profile=s.profile, policy=s.policy, tenant=s.tenant,
                       evidence=s.evidence, control=s.control, secrets=s.secrets).enter_session()
        content = s.surface.page.frame(name="content")
        await content.evaluate("() => { location.href = '/admin/gl'; }")
        await asyncio.sleep(0.5)
        assert "/admin/gl" not in content.url
    log = (s.evidence.dir / "events.jsonl").read_text()
    assert "policy.network_blocked" in log and "/admin/gl" in log


# ----------------------------------------------------------------------------- leak canary (keep last)
SEEDED_PII = ["JANE Q SAMPLE", "900-12-3456", "1200 EXAMPLE AVE", "608-555-0142", "04/17/1986",
              "ROBERT T EXAMPLE", "MARIA L TESTCASE", "demo-only-pw", "2,418.37", "2418.37"]


def test_zz_no_seeded_pii_or_secrets_in_any_evidence(runs_dir: Path):
    """Every text file every test wrote (events, results, interventions, DOM dumps) is scanned."""
    scanned, hits = 0, []
    for f in runs_dir.rglob("*"):
        if f.is_file() and f.suffix in {".jsonl", ".json", ".html"}:
            scanned += 1
            text = f.read_text(errors="ignore")
            hits += [(str(f.relative_to(runs_dir)), s) for s in SEEDED_PII if s in text]
    assert scanned > 20
    assert hits == []
