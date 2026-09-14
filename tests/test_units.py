"""Fast unit tests: schema, policy, redaction, values, registry composition, recorder, control transfer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from rote.control import ControlChannel, ControlViolation, Holder
from rote.discovery.recorder import RecordedAction, ScreenFacts, Trace, canonical_heading, synthesize
from rote.evidence import Evidence
from rote.policy import Policy
from rote.redact import redact_cells, redact_obj, redact_page_text, redact_text, redact_value
from rote.registry import ROOT, Registry, merge_patch, version_in_range
from rote.schema.capability import (
    Capability,
    FrameScope,
    InputSpec,
    LabelLocator,
    Literal_,
    OutputSpec,
    ParamRef,
    RoleLocator,
    TableCellLocator,
    Target,
)
from rote.schema.tenant import Overlay, Tenant
from rote.values import parse_output, validate_inputs

REG = Registry(ROOT)
LABELS = ["NAME", "SSN", "DOB", "ADDRESS", "PHONE"]


# ----------------------------------------------------------------------------- schema
def test_committed_capabilities_validate():
    caps = {c.id for c in REG.all()}
    assert {"keystone.coreserv.member_savings_balance", "keystone.coreserv.open_sub_account_review",
            "keystone.coreserv.open_sub_account", "keystone.coreserv.signon"} <= caps


def _cap_dict() -> dict:
    return REG.load("keystone.coreserv.member_savings_balance").model_dump(mode="json", by_alias=True)


def test_referential_integrity_rejects_unknown_target():
    d = _cap_dict()
    d["steps"][0]["target"] = "nope"
    with pytest.raises(ValidationError, match="unknown target"):
        Capability.model_validate(d)


def test_irreversible_step_requires_irreversible_capability():
    d = _cap_dict()
    d["steps"][0]["risk"] = "irreversible"
    with pytest.raises(ValidationError, match="irreversible"):
        Capability.model_validate(d)


def test_secret_inputs_cannot_come_from_caller():
    with pytest.raises(ValidationError):
        InputSpec(type="string", description="pw", classification="secret", source="caller")


def test_procedure_digest_tracks_execution_not_prose():
    cap = REG.load("keystone.coreserv.member_savings_balance")
    base = cap.procedure_digest()
    prose = cap.model_copy(deep=True)
    prose.title = "different"
    prose.targets["member_number_input"].description = "different"
    assert prose.procedure_digest() == base
    changed = cap.model_copy(deep=True)
    changed.targets["member_number_input"].locators = [LabelLocator(role="textbox", label="Other:")]
    assert changed.procedure_digest() != base


def test_json_schema_exports():
    schema = Capability.model_json_schema(by_alias=True)
    assert "steps" in schema["properties"] and "targets" in schema["properties"]


# ----------------------------------------------------------------------------- policy
@pytest.fixture
def policy() -> Policy:
    return Policy.load(ROOT / "policies" / "default.yaml", REG.profile("keystone.coreserv").risk,
                       extra_origins=["http://127.0.0.1:8601"])


def test_allowlist(policy: Policy):
    assert policy.allows_url("http://127.0.0.1:8601/mi/search")[0]
    assert not policy.allows_url("http://127.0.0.1:8601/admin/gl")[0]
    assert not policy.allows_url("http://127.0.0.1:8601/__faults")[0]
    assert not policy.allows_url("https://evil.example/mi/search")[0]
    assert not policy.allows_url("http://127.0.0.1:8601/unlisted")[0]


@pytest.mark.parametrize("name,role,submit,expected", [
    ("CONFIRM AND OPEN", "button", True, "irreversible"),
    ("CLOSE MEMBERSHIP", "button", True, "irreversible"),
    ("Approve wire", "button", False, "irreversible"),
    ("SEARCH", "clickable", False, "safe"),
    ("CONTINUE", "button", True, "safe"),
    ("SOMETHING NEW", "button", True, "irreversible"),  # unclassified submit: conservative
])
def test_click_risk(policy: Policy, name, role, submit, expected):
    assert policy.classify("click", name=name, role=role, is_submit=submit) == expected


def test_mode_asymmetry(policy: Policy):
    d = policy.check("discovery", "click", name="CONFIRM AND OPEN", role="button", is_submit=True)
    assert not d.allowed
    r = policy.check("replay", "click", name="CONFIRM AND OPEN", role="button", is_submit=True)
    assert r.allowed and r.needs_approval


def test_recorded_risk_is_never_downgraded(policy: Policy):
    d = policy.check("replay", "click", name="SEARCH", role="clickable", recorded_risk="irreversible")
    assert d.risk == "irreversible" and d.needs_approval


def test_link_outside_allowlist_blocked(policy: Policy):
    d = policy.check("discovery", "click", name="GENERAL LEDGER", role="link", href="http://127.0.0.1:8601/admin/gl")
    assert not d.allowed and "blocked" in d.reason


# ----------------------------------------------------------------------------- redaction
def test_pattern_redaction():
    t = redact_text("SSN 900-12-3456 phone 608-555-0142 dob 04/17/1986 bal $2,418.37 me@x.org", "log")
    assert "900-12" not in t and "555-0142" not in t and "04/17" not in t and "2,418" not in t and "me@" not in t
    assert "$2,418.37" in redact_text("bal $2,418.37", "model")  # the model may see the balance it must read


def test_capability_refs_are_not_mistaken_for_emails():
    assert redact_text("keystone.coreserv.signon@1.0.0") == "keystone.coreserv.signon@1.0.0"


def test_label_aware_redaction_catches_free_text_pii():
    assert redact_cells(["NAME", "JANE Q SAMPLE", "", "DOB", "x"], LABELS)[1] != "JANE Q SAMPLE"
    out = redact_page_text("NAME\tJANE Q SAMPLE\nADDRESS: 1 MAIN ST\nDESCRIPTION\tREGULAR SHARES", LABELS)
    assert "JANE" not in out and "MAIN ST" not in out and "REGULAR SHARES" in out


def test_value_and_object_redaction():
    assert redact_value("hunter2", "secret") == "[secret]"
    assert redact_value("2418.37", "financial", "log") == "[financial]"
    assert redact_obj({"password": "x", "note": "ssn 900-12-3456"}) == {"password": "[secret]", "note": "ssn ***-**-3456"}


# ----------------------------------------------------------------------------- values
def test_input_validation():
    cap = REG.load("keystone.coreserv.open_sub_account_review")
    ok = {"member_number": "48213", "product": "HOLIDAY CLUB", "nickname": "X", "opening_deposit": "10.00",
          "fund_from_suffix": "0000"}
    assert validate_inputs(cap, ok) == []
    bad = validate_inputs(cap, {**ok, "member_number": "12A45", "product": "GOLD", "extra": "1"})
    assert {p["input"] for p in bad} == {"member_number", "product", "extra"}
    signon = REG.load("keystone.coreserv.signon")
    assert validate_inputs(signon, {"password": "x"})[0]["problem"].startswith("secret")


@pytest.mark.parametrize("text,expected", [("$2,418.37", "2418.37"), ("($12.00)", "-12.00"), ("45.10 DR", "-45.10"),
                                           ("1,000.00 CR", "1000.00")])
def test_money_parsing(text, expected):
    assert parse_output(OutputSpec(type="money", description=""), text) == expected


def test_money_parsing_rejects_garbage():
    with pytest.raises(ValueError):
        parse_output(OutputSpec(type="money", description=""), "N/A")


# ----------------------------------------------------------------------------- registry / overlays
def test_version_ranges():
    assert version_in_range("4.3.0", ">=4.2,<5")
    assert not version_in_range("5.0.0", ">=4.2,<5")
    assert not version_in_range("4.1.9", ">=4.2,<5")


def test_merge_patch_semantics():
    assert merge_patch({"a": {"b": 1, "c": 2}, "l": [1, 2]}, {"a": {"b": 9, "c": None}, "l": [3]}) == \
        {"a": {"b": 9}, "l": [3]}


def test_overlay_applies_and_is_bounded():
    cap = REG.load("keystone.coreserv.member_savings_balance")
    lakeshore = REG.tenant("lakeshore")
    eff = REG.effective(cap, lakeshore)
    assert eff.applied_overlays
    assert eff.capability.targets["member_number_input"].locators[0].label == "Member #:"
    assert eff.capability.steps == cap.steps  # overlays never touch the step sequence
    evil = lakeshore.model_copy(update={"overlays": [Overlay(base=cap.id, base_versions=">=1.0.0,<2", reason="x",
                                                             patch={"steps": []})]})
    with pytest.raises(ValueError, match="may only patch"):
        REG.effective(cap, evil)
    old = lakeshore.model_copy(update={"overlays": [Overlay(base=cap.id, base_versions=">=2.0.0", reason="x",
                                                            patch={})]})
    with pytest.raises(ValueError, match="covers"):
        REG.effective(cap, old)


def test_overlay_cannot_launder_an_unapproved_change():
    cap = REG.load("keystone.coreserv.member_savings_balance").model_copy(deep=True)
    cap.targets["search_clickable"].locators = [RoleLocator(role="button", name="SEARCH")]  # edited after approval
    eff = REG.effective(cap, REG.tenant("lakeshore"))
    assert eff.capability.review.status == "draft"


# ----------------------------------------------------------------------------- recorder
def test_canonical_heading_strips_record_data():
    assert canonical_heading("MEMBER DETAIL - 12345", ["12345"]) == "MEMBER DETAIL"
    assert canonical_heading("REVIEW NEW SUB-ACCOUNT", []) == "REVIEW NEW SUB-ACCOUNT"


def _trace() -> Trace:
    prof = REG.profile("keystone.coreserv")
    t = Trace(goal="Read balance for 777", app=prof, tenant="prairie", app_version="4.2.1", model="m", run_id="r")
    content = FrameScope(name="content")
    search = ScreenFacts(content, "/mi/search", "MEMBER INQUIRY")
    detail = ScreenFacts(content, "/mi/detail", "MEMBER DETAIL")
    field = Target(description="f", scope=content, locators=[LabelLocator(role="textbox", label="Member Number:")])
    button = Target(description="b", scope=content, locators=[RoleLocator(role="button", name="GO")])
    cell = Target(description="c", scope=content, locators=[TableCellLocator(row_key="REGULAR SHARES", column="Bal")])
    t.inputs["member"] = InputSpec(type="string", description="m")
    t.input_values["member"] = "777"
    t.outputs["balance"] = OutputSpec(type="money", description="b", classification="financial")
    t.actions = [
        RecordedAction("fill", "type wrong", search, None, "reversible", field, value=Literal_(literal="x")),
        RecordedAction("fill", "type member", search, None, "reversible", field, value=ParamRef(param="member")),
        RecordedAction("click", "go", search, detail, "safe", button),
        RecordedAction("extract", "read", detail, None, "safe", cell, output="balance"),
    ]
    t.success_facts = detail
    return t


def test_synthesize_builds_screens_expectations_and_collapses_refills(tmp_path: Path):
    cap = synthesize(_trace(), "keystone.coreserv.test_cap", Registry(tmp_path))
    assert [s.action for s in cap.steps] == ["fill", "click", "extract"]  # superseded fill dropped
    assert cap.steps[1].expect.screen == "member_detail"
    assert cap.entry_screen == "member_inquiry" and cap.success.screen == "member_detail"
    assert cap.risk == "safe" and cap.review.status == "draft"
    assert cap.provenance.goal == "Read balance for {member}"  # argument values are not persisted


# ----------------------------------------------------------------------------- control transfer
@pytest.fixture
def channel(tmp_path: Path) -> ControlChannel:
    return ControlChannel("s", Evidence(tmp_path, "run"))


async def test_escalate_claim_release(channel: ControlChannel):
    async def operator():
        while channel.current is None:
            await asyncio.sleep(0.01)
        with pytest.raises(ControlViolation):
            channel.assert_human("forged")
        token = channel.claim(channel.current.id, "op")
        with pytest.raises(ControlViolation):
            channel.assert_automation()  # automation can't act while a human holds control
        channel.record_human({"channel": "console", "input": "click"})
        with pytest.raises(ControlViolation):
            channel.release(token, "approve")  # not an allowed decision for this intervention
        channel.release(token, "resume", "done")

    task = asyncio.create_task(operator())
    iv = await channel.escalate("unknown_state", "why", {}, None, timeout_s=5)
    await task
    assert iv.decision == "resume" and len(iv.human_actions) == 1
    assert channel.holder is Holder.AUTOMATION and channel.epoch == 3


async def test_unclaimed_escalation_expires(channel: ControlChannel):
    iv = await channel.escalate("stuck", "why", {}, None, timeout_s=0.05)
    assert iv.status == "expired" and channel.holder is Holder.AUTOMATION


async def test_claimed_escalation_is_not_yanked_back_on_timeout(channel: ControlChannel):
    async def slow_operator():
        while channel.current is None:
            await asyncio.sleep(0.01)
        token = channel.claim(channel.current.id, "op")
        await asyncio.sleep(0.2)  # longer than the escalation timeout
        channel.release(token, "resume")

    task = asyncio.create_task(slow_operator())
    iv = await channel.escalate("stuck", "why", {}, None, timeout_s=0.05)
    await task
    assert iv.status == "resolved"


async def test_epoch_guard_detects_transfer_between_decide_and_act(channel: ControlChannel):
    epoch = channel.epoch
    channel.request_preempt("op")
    task = asyncio.create_task(channel.yield_if_preempted({}, None, timeout_s=0.05))
    await task
    with pytest.raises(ControlViolation):
        channel.assert_automation(epoch)


def test_tenant_model_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Tenant.model_validate({"id": "x", "display_name": "x", "app": "a", "app_version": "1", "base_url": "u",
                               "surprise": 1})


def test_bare_id_prefers_newest_approved_version(tmp_path: Path):
    import shutil
    for d in ("apps", "capabilities", "policies", "tenants"):
        shutil.copytree(ROOT / d, tmp_path / d)
    reg = Registry(tmp_path)
    cap = reg.load("keystone.coreserv.member_savings_balance").model_copy(deep=True)
    cap.version, cap.review.status = "1.1.0", "draft"
    reg.save(cap)
    assert reg.load("keystone.coreserv.member_savings_balance").version == "1.0.0"
    assert reg.load("keystone.coreserv.member_savings_balance@1.1.0").review.status == "draft"
