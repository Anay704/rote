"""Turns a discovery trace into a capability artifact.

The recorder is deliberately model-free: it only sees what was *done* (hardened targets, bound
values, screen facts before/after), never the model's reasoning. That is what decouples the
artifact from the transcript. The transcript is kept as evidence, not as input to replay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rote.evidence import now_iso
from rote.registry import Registry
from rote.schema.capability import (
    AppRef,
    Capability,
    ClickStep,
    Expectation,
    ExtractStep,
    FillStep,
    FrameScope,
    InputSpec,
    Literal_,
    ManualStep,
    Outcome,
    OutputSpec,
    PageScope,
    ParamRef,
    PressStep,
    Provenance,
    Review,
    Scope,
    Screen,
    SelectStep,
    SuccessCriteria,
    Target,
    TextRule,
    UrlPathRule,
)
from rote.schema.profile import AppProfile

ORDER = {"safe": 0, "reversible": 1, "irreversible": 2}


@dataclass(frozen=True)
class ScreenFacts:
    scope: Scope
    path: str
    heading: str  # first visible line of the scope, canonicalised

    @property
    def key(self) -> tuple[str, str, str]:
        name = self.scope.name if isinstance(self.scope, FrameScope) else "page"
        return (name or "", self.path, self.heading)


def canonical_heading(line: str, literals: list[str]) -> str:
    """Screen headings often embed record data ("MEMBER DETAIL - 12345"). Keep the stable prefix."""
    for lit in sorted(literals, key=len, reverse=True):
        if lit:
            line = line.replace(lit, " ")
    line = re.split(r"\d", line, maxsplit=1)[0]
    return " ".join(line.split()).strip(" -:#|")


@dataclass
class RecordedAction:
    kind: str  # click | fill | select | press | extract | manual
    intent: str
    before: ScreenFacts
    after: ScreenFacts | None
    risk: str
    target: Target | None = None
    target_hint: str = ""
    value: Any = None  # ParamRef | Literal_
    key: str | None = None
    output: str | None = None
    origin: str = "model"
    instructions: str = ""
    match: str = "label"


@dataclass
class Trace:
    goal: str
    app: AppProfile
    tenant: str
    app_version: str
    model: str
    run_id: str
    inputs: dict[str, InputSpec] = field(default_factory=dict)
    input_values: dict[str, str] = field(default_factory=dict)  # in memory only; never persisted
    outputs: dict[str, OutputSpec] = field(default_factory=dict)
    actions: list[RecordedAction] = field(default_factory=list)
    success_facts: ScreenFacts | None = None
    title: str = ""
    description: str = ""


def capability_risk(steps) -> str:
    """Capability risk describes effects on the system of record. Filling a form is reversible UI state and
    does not raise it; a committing click or a manual step does."""
    effective = [s.risk for s in steps if s.action not in ("fill", "select")]
    return "irreversible" if "irreversible" in effective else "safe"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "screen"


def _target_id(desc: str, used: set[str]) -> str:
    base = "_".join(_slug(desc).split("_")[:5])
    tid, n = base, 2
    while tid in used:
        tid, n = f"{base}_{n}", n + 1
    used.add(tid)
    return tid


def _collapse(actions: list[RecordedAction]) -> list[RecordedAction]:
    """Drop superseded fills: if the same control was filled again before any transition, keep the last."""
    out: list[RecordedAction] = []
    for a in actions:
        if a.kind in ("fill", "select") and a.target is not None:
            for j in range(len(out) - 1, -1, -1):
                prev = out[j]
                if prev.after is not None and prev.after.key != prev.before.key:
                    break  # a transition happened; earlier fills are part of a different screen visit
                if prev.kind == a.kind and prev.target == a.target:
                    out.pop(j)
                    break
        out.append(a)
    return out


def _template(trace: Trace) -> str:
    """The goal with concrete argument values replaced by {param} placeholders (values aren't persisted)."""
    goal = trace.goal
    for name, value in sorted(trace.input_values.items(), key=lambda kv: -len(kv[1])):
        if value:
            goal = re.sub(re.escape(value), "{" + name + "}", goal, flags=re.I)
    return goal


def synthesize(trace: Trace, cap_id: str, registry: Registry) -> Capability:
    actions = _collapse(trace.actions)
    if not actions:
        raise ValueError("nothing to record")

    screens: dict[str, Screen] = {}
    screen_ids: dict[tuple[str, str, str], str] = {}

    def screen_for(f: ScreenFacts) -> str:
        if f.key in screen_ids:
            return screen_ids[f.key]
        sid = _slug(f.heading or f.path)
        while sid in screens:
            sid += "_2"
        # The frame is found by name; the url_path rule is the actual assertion, so don't repeat it in the scope.
        scope = FrameScope(name=f.scope.name) if isinstance(f.scope, FrameScope) and f.scope.name else f.scope
        rules: list = [UrlPathRule(scope=scope, path=f.path)]
        if f.heading:
            rules.append(TextRule(scope=scope, text=f.heading))
        scope_desc = f"frame '{f.scope.name}'" if isinstance(f.scope, FrameScope) else "page"
        screens[sid] = Screen(description=f"{f.heading or f.path} ({scope_desc}, {f.path})", identify=rules)
        screen_ids[f.key] = sid
        return sid

    targets: dict[str, Target] = {}
    target_ids: dict[str, str] = {}
    used: set[str] = set()
    steps = []
    for n, a in enumerate(actions, 1):
        on = screen_for(a.before)
        expect = None
        if a.after is not None and a.after.key != a.before.key:
            expect = Expectation(screen=screen_for(a.after))
        sid = f"s{n:02d}_{'_'.join(_slug(a.intent).split('_')[:4])}"
        if a.kind == "manual":
            steps.append(ManualStep(id=sid, intent=a.intent, screen=on, instructions=a.instructions, expect=expect,
                                    origin="human"))
            continue
        assert a.target is not None
        tkey = a.target.model_dump_json()
        if tkey not in target_ids:
            tid = _target_id(a.target_hint or a.target.description, used)
            target_ids[tkey] = tid
            targets[tid] = a.target
        tid = target_ids[tkey]
        common = dict(id=sid, intent=a.intent, screen=on, target=tid, origin=a.origin)
        if a.kind == "click":
            steps.append(ClickStep(**common, risk=a.risk, expect=expect))
        elif a.kind == "fill":
            steps.append(FillStep(**common, value=a.value, risk=a.risk, expect=expect))
        elif a.kind == "select":
            steps.append(SelectStep(**common, value=a.value, match=a.match, risk=a.risk, expect=expect))
        elif a.kind == "press":
            steps.append(PressStep(**common, key=a.key or "Enter", risk=a.risk, expect=expect))
        elif a.kind == "extract":
            steps.append(ExtractStep(**common, output=a.output or ""))

    success_screen = screen_for(trace.success_facts) if trace.success_facts else steps[-1].screen
    risk = capability_risk(steps)
    human_steps = sum(1 for s in steps if s.origin == "human")

    versions = registry.versions(cap_id)
    if versions:
        major, minor, _ = (int(x) for x in versions[-1].split("."))
        version = f"{major}.{minor + 1}.0"
    else:
        version = "1.0.0"

    major_minor = ".".join(trace.app_version.split(".")[:2])
    outcomes = [Outcome(code=c.code, description=c.description) for c in trace.app.conditions
                if c.klass == "business_outcome"]
    select_steps = [s.id for s in steps if isinstance(s, SelectStep) and isinstance(s.value, ParamRef)]
    if select_steps:
        outcomes.append(Outcome(code="OPTION_UNAVAILABLE", steps=select_steps,
                                description="The screen does not offer the requested option for this record."))
    return Capability(
        id=cap_id,
        version=version,
        title=trace.title or _template(trace)[:80],
        description=trace.description or _template(trace),
        app=AppRef(product=trace.app.id, compatible_versions=f">={major_minor},<{int(major_minor.split('.')[0]) + 1}",
                   surface=trace.app.surface),
        risk=risk,
        inputs=trace.inputs,
        outputs=trace.outputs,
        outcomes=outcomes,
        entry_screen=steps[0].screen,
        screens=screens,
        targets=targets,
        steps=steps,
        success=SuccessCriteria(screen=success_screen, outputs_required=list(trace.outputs)),
        review=Review(status="draft", notes=("Recorded from a model-driven discovery run. Business outcomes are "
                                             "inherited from the app profile; narrow them during review."
                                             + (f" Contains {human_steps} human step(s)." if human_steps else ""))),
        provenance=Provenance(method="discovered", run_id=trace.run_id, recorded_at=now_iso(),
                              recorded_on={"tenant": trace.tenant, "app_version": trace.app_version},
                              model=trace.model, goal=_template(trace), human_steps=human_steps),
    )


__all__ = ["RecordedAction", "ScreenFacts", "Trace", "canonical_heading", "synthesize", "Literal_", "ParamRef",
           "PageScope"]
