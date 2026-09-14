"""The human review pass applied to discovered drafts before approval (kept as code so it is reviewable).

What the reviewer changed, and why:
  * member_number: constrained to the host's format (1-10 digits) so malformed ids are rejected as
    INVALID_INPUT before the UI is touched, instead of round-tripping to a host validation message.
  * review notes record what was checked.
Nothing here touches steps, targets or screens, so the verification replay's procedure digest still holds.
"""

from rote.registry import Registry

r = Registry()
for cap_id in ("keystone.coreserv.member_savings_balance", "keystone.coreserv.open_sub_account_review"):
    cap = r.load(cap_id)
    before = cap.procedure_digest()
    cap.inputs["member_number"].pattern = r"^\d{1,10}$"
    cap.review.notes = ("Reviewed: locators, screens and step intents checked against the recording screenshots; "
                        "member_number constrained to 1-10 digits (host format); business outcomes confirmed "
                        "against the app profile.")
    assert cap.procedure_digest() == before
    r.save(cap)
    print(f"reviewed {cap.ref} (procedure digest {before} unchanged)")
