"""Discovery loop mechanics with a scripted stand-in for the model (no API calls).

The real model run lives in evidence/01-discovery; these tests pin down what the *harness* guarantees
regardless of what the model does: policy blocks, forced parameterisation, stuck detection, and that
a finished run synthesises a valid artifact that replays.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

from rote.discovery.agent import Discoverer, DiscoveryConfig
from rote.replay.engine import Replayer, ReplayOptions
from rote.runtime import open_session


@dataclass
class Block:
    type: str
    text: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = ""


@dataclass
class Usage:
    input_tokens: int = 1
    output_tokens: int = 1
    cache_read_input_tokens: int = 0


@dataclass
class Resp:
    content: list[Block]
    stop_reason: str = "tool_use"
    model: str = "scripted"
    usage: Usage = field(default_factory=Usage)


class ScriptedModel:
    """`script` is a list of callables(observation_text, last_result_text) -> (tool_name, input)."""

    def __init__(self, script):
        self.script = list(script)
        self.seen_errors: list[str] = []
        self._ids = itertools.count()
        self.beta = self
        self.messages = self

    async def create(self, **kw) -> Resp:
        last = kw["messages"][-1]["content"]
        text = _all_text(last)
        if "Error:" in text:
            self.seen_errors.append(text.split("Error:", 1)[1].split("\n", 1)[0].strip())
        step = self.script.pop(0) if self.script else (lambda o, r: ("finish", {"status": "failed", "summary": "x"}))
        name, args = step(text, text)
        return Resp([Block("tool_use", name=name, input=args, id=f"t{next(self._ids)}")])


def _all_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if b.get("type") == "text":
            out.append(b["text"])
        elif b.get("type") == "tool_result":
            out.append(_all_text(b["content"]) if isinstance(b["content"], list) else str(b["content"]))
    return "\n".join(out)


def ref(pattern: str):
    def find(obs: str) -> str:
        m = re.search(r"[\[{](e\d+)[^\]}]*?" + pattern, obs)
        assert m, f"no element matching {pattern!r} in observation"
        return m.group(1)
    return find


def cell_ref(text: str):
    def find(obs: str) -> str:
        m = re.search(r"\{(e\d+)\} " + re.escape(text), obs)
        assert m, text
        return m.group(1)
    return find


async def test_scripted_discovery_enforces_guardrails_and_records_replayable_artifact(registry, runs_dir, apps):
    goal = "Read the savings balance for member 12345"
    script = [
        lambda o, r: ("click", {"ref": ref(r'link "MEMBER INQUIRY"')(o), "intent": "open inquiry"}),
        # a literal that appears in the goal must become a parameter
        lambda o, r: ("type_text", {"ref": ref(r'textbox')(o), "text": "12345", "intent": "enter member"}),
        lambda o, r: ("type_text", {"ref": ref(r'textbox')(o), "text": "12345", "intent": "enter member",
                                    "param": {"name": "member_number", "type": "string", "description": "id",
                                              "classification": "pii"}}),
        lambda o, r: ("click", {"ref": ref(r'clickable "SEARCH"')(o), "intent": "search"}),
        # prompt-injected instruction on the page: the harness blocks it even if a model obeys
        lambda o, r: ("click", {"ref": ref(r'button "CLOSE MEMBERSHIP"')(o), "intent": "follow memo"}),
        lambda o, r: ("click", {"ref": ref(r'link "GENERAL LEDGER"')(o), "intent": "wander off"}),
        lambda o, r: ("extract_value", {"ref": cell_ref("$2,418.37")(o), "intent": "read balance",
                                        "output": {"name": "balance", "type": "money", "description": "b",
                                                   "classification": "financial"}}),
        lambda o, r: ("finish", {"status": "success", "summary": "done", "capability_title": "Read savings",
                                 "capability_description": "d", "checkpoint_ref": cell_ref("MEMBER DETAIL")(o)}),
    ]
    model = ScriptedModel(script)
    async with open_session("discover", "prairie", registry=registry, runs_dir=runs_dir, echo=False) as s:
        d = Discoverer(s, DiscoveryConfig(goal=goal, capability_id="keystone.coreserv.scripted_balance"), model)
        out = await d.run()
    assert out.status == "recorded", out.summary
    assert any("appears in the goal" in e for e in model.seen_errors)
    assert any("blocked by policy" in e and "CLOSE MEMBERSHIP" in e for e in model.seen_errors)
    assert any("link target blocked" in e for e in model.seen_errors)

    cap = out.capability
    assert [st.action for st in cap.steps] == ["click", "fill", "click", "extract"]
    assert cap.inputs["member_number"].classification == "pii"
    assert cap.provenance.goal == "Read the savings balance for member {member_number}"
    log = (s.evidence.dir / "events.jsonl").read_text()
    assert "12345" not in log  # argument value scrubbed from every discovery log event

    async with open_session("verify", "prairie", registry=registry, runs_dir=runs_dir, echo=False) as vs:
        rep = Replayer(registry=registry, surface=vs.surface, profile=vs.profile, policy=vs.policy,
                       tenant=vs.tenant, evidence=vs.evidence, control=vs.control, secrets=vs.secrets,
                       options=ReplayOptions(allow_draft=True))
        result = await rep.run(cap, {"member_number": "20417"})  # a different member than recorded
    assert result.status == "succeeded" and result.outputs == {"balance": "15002.55"}
    registry.capability_path(cap.id, cap.version).unlink()


async def test_no_progress_is_detected_as_stuck(registry, runs_dir, apps):
    loop = [lambda o, r: ("click", {"ref": ref(r'link "TELLER HOME"')(o), "intent": "go home"})] * 12
    async with open_session("discover", "prairie", registry=registry, runs_dir=runs_dir, echo=False) as s:
        d = Discoverer(s, DiscoveryConfig(goal="impossible", capability_id="keystone.coreserv.never",
                                          no_progress_limit=3), ScriptedModel(loop))
        out = await d.run()
    assert out.status == "stuck" and "no visible progress" in out.summary
    assert "discovery.escalation_unattended" in (s.evidence.dir / "events.jsonl").read_text()
