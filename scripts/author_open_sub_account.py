"""Author keystone.coreserv.open_sub_account: the discovered review flow + a human-written commit step.

Discovery is never allowed to commit (policy blocks irreversible actions in discovery mode), so a flow
that commits is always partly authored. The authored step is still gated per execution by approval.
"""

from rote.evidence import now_iso
from rote.registry import Registry
from rote.schema.capability import (
    ClickStep,
    CssLocator,
    Expectation,
    ExtractStep,
    FrameScope,
    OutputSpec,
    Provenance,
    Review,
    RoleLocator,
    Screen,
    Target,
    TextRule,
    UrlPathRule,
)

r = Registry()
base = r.load("keystone.coreserv.open_sub_account_review")
cap = base.model_copy(deep=True)
cap.id, cap.version = "keystone.coreserv.open_sub_account", "1.0.0"
cap.title = "Open a member sub-account (commits)"
cap.description = (f"Runs the steps of {base.ref}, then confirms the opening. The confirm step is irreversible and "
                   "pauses for human approval on every execution. Returns the host confirmation message.")
cap.risk = "irreversible"
content = FrameScope(name="content")
cap.screens["sub_account_opened"] = Screen(
    description="SUB-ACCOUNT OPENED (frame 'content', /sa/commit)",
    identify=[UrlPathRule(scope=content, path="/sa/commit"), TextRule(scope=content, text="SUB-ACCOUNT OPENED")])
cap.targets["confirm_and_open_button"] = Target(
    description="button 'CONFIRM AND OPEN'", scope=FrameScope(name="content", url_path="/sa/review"),
    locators=[RoleLocator(role="button", name="CONFIRM AND OPEN")],
    rationale="Authored. One semantic locator on purpose: for an irreversible control no fallback beats a wrong one.")
cap.targets["confirmation_message"] = Target(
    description="confirmation message", scope=FrameScope(name="content", url_path="/sa/commit"),
    locators=[CssLocator(selector="body > table > tbody > tr:nth-of-type(2) > td")],
    rationale="Authored. Free text in the page body with no label or grid; structural path accepted and noted.")
review_screen = base.success.screen
cap.steps.append(ClickStep(id="s10_confirm_and_open", intent="Confirm and open the sub-account", screen=review_screen,
                           target="confirm_and_open_button", risk="irreversible", origin="author",
                           expect=Expectation(screen="sub_account_opened")))
cap.outputs["confirmation"] = OutputSpec(type="string", classification="internal",
                                         description="Host confirmation message incl. new suffix and confirmation #")
cap.steps.append(ExtractStep(id="s11_read_confirmation", intent="Read the confirmation message",
                             screen="sub_account_opened", target="confirmation_message", output="confirmation",
                             origin="author"))
cap.success.screen, cap.success.outputs_required = "sub_account_opened", ["confirmation"]
cap.provenance = Provenance(method="authored", recorded_at=now_iso(),
                            recorded_on={"derived_from": base.ref, "tenant": "prairie", "app_version": "4.2.1"})
cap.review = Review(status="approved", approved_by="reviewer.anay", approved_at=now_iso(),
                    notes="Authored extension of a discovered flow; the commit step still needs per-execution approval.")
cap.review.approved_digest = cap.procedure_digest()
print(r.save(cap))
