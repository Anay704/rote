"""The capability artifact: a typed, versioned, reviewable description of a recorded flow.

Design notes (see REPORT.md section 2 for the long form):

* A capability is a *contract first* (inputs, outputs, outcomes, risk) and a *procedure second*
  (screens, targets, steps). A calling agent only needs the contract; a reviewer reads both.
* Targets live in a keyed table, not inline in steps. Steps reference them by id. That makes
  targets the unit of per-tenant override and of drift reporting ("target member_number_input
  failed on lakeshore") without touching the step sequence.
* Each target carries an ordered list of locator strategies, most semantic first. Replay requires
  a unique match and flags any fallback use as degradation.
* Screens are explicit states with identification rules. Steps say which screen they act on and
  which screen they expect next, which gives replay a state-based wait (never a sleep) and a way to
  re-synchronise after a human hands control back.
* Nothing surface-specific leaks above `Scope` and `Locator`: a desktop surface would add a
  `window` scope and e.g. a UIA `automation_id` strategy; steps, screens, IO and outcomes stay put.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "rote.capability/v1"

Classification = Literal["public", "internal", "financial", "pii", "secret"]
"""Data sensitivity. Drives redaction in logs/evidence and whether a value may be supplied by the
caller (`secret` values are only ever resolved from a secret provider, never passed as arguments)."""

Risk = Literal["safe", "reversible", "irreversible"]
"""safe: no state change (navigate, read). reversible: changes UI-local state only (typing into a
form that is not yet submitted). irreversible: commits a change in the system of record."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- IO contract

class InputSpec(Strict):
    type: Literal["string", "integer", "money", "enum", "date"]
    description: str
    classification: Classification = "internal"
    required: bool = True
    pattern: str | None = Field(None, description="Regex the raw string form must fully match.")
    values: list[str] | None = Field(None, description="Allowed values for enum inputs.")
    source: Literal["caller", "secret"] = "caller"

    @model_validator(mode="after")
    def _check(self) -> InputSpec:
        if self.type == "enum" and not self.values:
            raise ValueError("enum inputs need `values`")
        if self.classification == "secret" and self.source != "secret":
            raise ValueError("secret inputs must use source=secret; callers never pass secrets")
        if self.pattern:
            re.compile(self.pattern)
        return self


class OutputSpec(Strict):
    type: Literal["string", "integer", "money", "date"]
    description: str
    classification: Classification = "internal"


# --------------------------------------------------------------------------- targeting

class FrameScope(Strict):
    """Where a control lives. For web: a frame, matched by name, then URL path as fallback."""
    kind: Literal["frame"] = "frame"
    name: str | None = None
    url_path: str | None = None


class PageScope(Strict):
    kind: Literal["page"] = "page"


Scope = Annotated[Union[FrameScope, PageScope], Field(discriminator="kind")]


class RoleLocator(Strict):
    """Accessibility role + accessible name. Most semantic; survives markup/CSS churn and exists on
    desktop accessibility trees (UIA/AX) too. Useless when the control has no accessible name."""
    strategy: Literal["role"] = "role"
    role: str
    name: str


class LabelLocator(Strict):
    """A control of `role` positioned right of / below the visible text `label`. This is how an
    operator finds a field on a table-layout screen with no <label for>. Geometry-based, so it also
    maps onto screenshot+OCR surfaces."""
    strategy: Literal["label"] = "label"
    role: str
    label: str


class TextLocator(Strict):
    """A clickable element whose visible text equals `text`. Covers non-semantic 'buttons'
    (<td onclick>) that have no role."""
    strategy: Literal["text"] = "text"
    text: str
    role: str | None = None


class TableCellLocator(Strict):
    """A data cell addressed like a human reads a grid: the row whose cell equals `row_key`, in the
    column headed `column`. Survives row reordering and added rows. With `column: null` it addresses
    a key/value grid: the first non-empty cell after the `row_key` cell."""
    strategy: Literal["table_cell"] = "table_cell"
    row_key: str
    column: str | None = None


class CssLocator(Strict):
    """Structural DOM path. Last resort, flagged brittle: legacy markup has no stable ids."""
    strategy: Literal["css"] = "css"
    selector: str


Locator = Annotated[
    Union[RoleLocator, LabelLocator, TextLocator, TableCellLocator, CssLocator],
    Field(discriminator="strategy"),
]


class Fingerprint(Strict):
    """What the control looked like when recorded. Not used to act; used to explain drift."""
    role: str
    name: str = ""
    label: str = ""
    text: str = ""
    bbox: tuple[float, float, float, float] | None = None


class Target(Strict):
    description: str
    scope: Scope = Field(default_factory=PageScope)
    locators: list[Locator] = Field(min_length=1)
    fingerprint: Fingerprint | None = None
    rationale: str | None = Field(None, description="Why these strategies, in this order.")


# --------------------------------------------------------------------------- screens

class UrlPathRule(Strict):
    kind: Literal["url_path"] = "url_path"
    scope: Scope = Field(default_factory=PageScope)
    path: str = Field(description="Exact path, query string ignored (canonical form).")


class TextRule(Strict):
    kind: Literal["text_present"] = "text_present"
    scope: Scope = Field(default_factory=PageScope)
    text: str


class TargetRule(Strict):
    kind: Literal["target_present"] = "target_present"
    target: str


ScreenRule = Annotated[Union[UrlPathRule, TextRule, TargetRule], Field(discriminator="kind")]


class Screen(Strict):
    description: str
    identify: list[ScreenRule] = Field(min_length=1, description="All rules must hold.")


# --------------------------------------------------------------------------- steps

class ParamRef(Strict):
    param: str


class Literal_(Strict):
    literal: str


class SecretRef(Strict):
    secret: str


ValueSource = Union[ParamRef, SecretRef, Literal_]


class Expectation(Strict):
    screen: str
    timeout_ms: int = 10000


class _StepBase(Strict):
    id: str
    intent: str = Field(description="What this step is for, in operator language.")
    screen: str = Field(description="Screen this step acts on; checked before acting.")
    origin: Literal["model", "human", "author"] = "model"


class ClickStep(_StepBase):
    action: Literal["click"] = "click"
    target: str
    risk: Risk = "safe"
    expect: Expectation | None = None


class FillStep(_StepBase):
    action: Literal["fill"] = "fill"
    target: str
    value: ValueSource
    risk: Risk = "reversible"
    expect: Expectation | None = None


class SelectStep(_StepBase):
    action: Literal["select"] = "select"
    target: str
    value: ValueSource
    match: Literal["label", "label_prefix", "value"] = Field(
        "label", description="label_prefix lets callers pass a stable code ('0000') for options like '0000 REGULAR SHARES'.")
    risk: Risk = "reversible"
    expect: Expectation | None = None


class PressStep(_StepBase):
    action: Literal["press"] = "press"
    target: str
    key: Literal["Enter", "Tab", "Escape"]
    risk: Risk = "safe"
    expect: Expectation | None = None


class ExtractStep(_StepBase):
    action: Literal["extract"] = "extract"
    target: str
    output: str
    risk: Risk = "safe"


class ManualStep(_StepBase):
    """A step a human performed during discovery. Replay escalates here instead of guessing."""
    action: Literal["manual"] = "manual"
    instructions: str
    risk: Risk = "irreversible"
    expect: Expectation | None = None


Step = Annotated[
    Union[ClickStep, FillStep, SelectStep, PressStep, ExtractStep, ManualStep],
    Field(discriminator="action"),
]


# --------------------------------------------------------------------------- envelope

class AppRef(Strict):
    product: str = Field(description="Vendor product id; conditions/auth come from its app profile.")
    compatible_versions: str = Field(description="Version range this flow is known to work on, e.g. '>=4.2,<5'.")
    surface: Literal["web", "desktop"] = "web"


class Provenance(Strict):
    method: Literal["discovered", "authored"]
    run_id: str | None = None
    recorded_at: str
    recorded_on: dict[str, str] = Field(default_factory=dict, description="tenant, app_version, ...")
    model: str | None = None
    goal: str | None = None
    human_steps: int = 0
    verification: dict | None = Field(None, description="Result of the post-record verification replay.")


class Review(Strict):
    status: Literal["draft", "approved", "deprecated"] = "draft"
    approved_by: str | None = None
    approved_at: str | None = None
    approved_digest: str | None = Field(None, description="procedure_digest() at approval time")
    notes: str | None = None


class Outcome(Strict):
    """A business outcome this capability can legitimately return. `code` must match a
    business_outcome condition in the app profile."""
    code: str
    description: str
    steps: list[str] = Field(default_factory=list, description="Steps after which it can occur (empty = any).")


class SuccessCriteria(Strict):
    screen: str
    outputs_required: list[str] = Field(default_factory=list)


class Capability(Strict):
    schema_version: Literal["rote.capability/v1"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str
    app: AppRef
    risk: Risk = Field(description="Worst effect on the system of record. Form filling alone does not raise it.")
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    outcomes: list[Outcome] = Field(default_factory=list)
    entry_screen: str
    screens: dict[str, Screen]
    targets: dict[str, Target]
    steps: list[Step] = Field(min_length=1)
    success: SuccessCriteria
    review: Review = Field(default_factory=Review)
    provenance: Provenance

    @model_validator(mode="after")
    def _referential_integrity(self) -> Capability:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        known_screens = set(self.screens)
        if self.entry_screen not in known_screens:
            raise ValueError(f"entry_screen {self.entry_screen!r} not in screens")
        if self.success.screen not in known_screens:
            raise ValueError(f"success.screen {self.success.screen!r} not in screens")
        for s in self.steps:
            if s.screen not in known_screens:
                raise ValueError(f"step {s.id}: unknown screen {s.screen!r}")
            exp = getattr(s, "expect", None)
            if exp and exp.screen not in known_screens:
                raise ValueError(f"step {s.id}: expects unknown screen {exp.screen!r}")
            tgt = getattr(s, "target", None)
            if tgt and tgt not in self.targets:
                raise ValueError(f"step {s.id}: unknown target {tgt!r}")
            val = getattr(s, "value", None)
            if isinstance(val, ParamRef) and val.param not in self.inputs:
                raise ValueError(f"step {s.id}: unknown input {val.param!r}")
            if isinstance(s, ExtractStep) and s.output not in self.outputs:
                raise ValueError(f"step {s.id}: unknown output {s.output!r}")
        for scr_id, scr in self.screens.items():
            for rule in scr.identify:
                if isinstance(rule, TargetRule) and rule.target not in self.targets:
                    raise ValueError(f"screen {scr_id}: unknown target {rule.target!r}")
        for o in self.success.outputs_required:
            if o not in self.outputs:
                raise ValueError(f"success requires unknown output {o!r}")
        if any(s.risk == "irreversible" for s in self.steps) and self.risk != "irreversible":
            raise ValueError("a capability with an irreversible step must declare risk: irreversible")
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def procedure_digest(self) -> str:
        """Hash of everything replay executes. Verification and approval are bound to it, so editing a step
        or locator after approval invalidates the approval; editing descriptions or review notes does not."""
        import hashlib
        import json
        body = self.model_dump(mode="json", include={"entry_screen", "screens", "targets", "steps", "success"})
        for t in body["targets"].values():
            t.pop("description", None), t.pop("rationale", None), t.pop("fingerprint", None)
        for s in body["steps"]:
            s.pop("intent", None)
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
