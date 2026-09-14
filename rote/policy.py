"""Guardrails: an explicit allowlist plus a risk model, enforced in code rather than in the prompt.

Enforcement points (defence in depth):
  * before every action (agent tool call, replay step, recovery click): action type allowed? risk?
  * before following a link: href inside the allowlist?
  * at the network layer: every request the browser makes is routed through `allows_url`, so a
    page-initiated navigation (script, injected link, redirect) outside the allowlist is aborted even
    if no action check caught it.

Risk handling is asymmetric by mode. Discovery *blocks* irreversible actions outright: an LLM
exploring a banking UI should never be the one to commit a change. Replay *pauses for approval*:
the step was reviewed as part of the capability, but each execution still needs a human decision.
Controls the policy cannot classify are treated as irreversible. False positives cost a human
click; false negatives cost a real transaction.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

from rote.schema.capability import Risk
from rote.schema.profile import RiskHints

Mode = Literal["discovery", "replay"]
ActionType = Literal["click", "fill", "select", "press", "extract", "navigate", "dismiss"]


class PolicyConfig(BaseModel):
    allowed_origins: list[str]
    allowed_paths: list[str]
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: dict[Mode, list[ActionType]]
    irreversible_patterns: list[str] = Field(default_factory=list)
    on_irreversible: dict[Mode, Literal["block", "require_approval"]] = Field(
        default_factory=lambda: {"discovery": "block", "replay": "require_approval"})
    treat_unclassified_submit_as: Risk = "irreversible"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    risk: Risk
    reason: str
    needs_approval: bool = False


class Policy:
    def __init__(self, cfg: PolicyConfig, hints: RiskHints | None = None):
        self.cfg = cfg
        self.hints = hints or RiskHints()
        self._irrev = [re.compile(p, re.I) for p in cfg.irreversible_patterns + self.hints.irreversible_controls]
        self._safe = [re.compile(p, re.I) for p in self.hints.safe_controls]

    @classmethod
    def load(cls, path: Path, hints: RiskHints | None = None, extra_origins: list[str] | None = None) -> Policy:
        cfg = PolicyConfig.model_validate(yaml.safe_load(path.read_text()))
        if extra_origins:
            cfg.allowed_origins = sorted(set(cfg.allowed_origins) | set(extra_origins))
        return cls(cfg, hints)

    # ---------------------------------------------------------------- allowlist
    def allows_url(self, url: str) -> tuple[bool, str]:
        if url.startswith(("about:", "data:", "blob:")):
            return True, "local"
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.cfg.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        path = parts.path or "/"
        if any(fnmatch.fnmatch(path, p) for p in self.cfg.denied_paths):
            return False, f"path {path} is explicitly denied"
        if not any(fnmatch.fnmatch(path, p) for p in self.cfg.allowed_paths):
            return False, f"path {path} not in allowlist"
        return True, "allowlisted"

    # ---------------------------------------------------------------- risk
    def classify(self, action: ActionType, *, name: str = "", role: str = "", is_submit: bool = False,
                 href: str | None = None) -> Risk:
        if action in ("extract", "navigate"):
            return "safe"
        if action in ("fill", "select"):
            return "reversible"
        label = name.strip()
        if any(p.search(label) for p in self._irrev):
            return "irreversible"
        if any(p.fullmatch(label) for p in self._safe):
            return "safe"
        if role == "link" and href and not href.lower().startswith("javascript:"):
            return "safe"  # plain GET navigation; the URL itself is allowlist-checked separately
        if action == "press" and not is_submit:
            return "safe"
        return self.cfg.treat_unclassified_submit_as

    def check(self, mode: Mode, action: ActionType, *, name: str = "", role: str = "", is_submit: bool = False,
              href: str | None = None, recorded_risk: Risk | None = None) -> Decision:
        if action not in self.cfg.allowed_actions.get(mode, []):
            return Decision(False, "irreversible", f"action type '{action}' not allowed in {mode}")
        if href and not href.lower().startswith(("javascript:", "#")):
            ok, why = self.allows_url(href)
            if not ok:
                return Decision(False, "irreversible", f"link target blocked: {why}")
        risk = self.classify(action, name=name, role=role, is_submit=is_submit, href=href)
        order = {"safe": 0, "reversible": 1, "irreversible": 2}
        if recorded_risk and order[recorded_risk] > order[risk]:
            risk = recorded_risk  # never downgrade what review recorded
        if risk == "irreversible":
            if self.cfg.on_irreversible[mode] == "block":
                return Decision(False, risk, f"'{label_of(name, role)}' is classified irreversible; "
                                             f"blocked in {mode}")
            return Decision(True, risk, "irreversible; requires human approval", needs_approval=True)
        return Decision(True, risk, "allowed")


def label_of(name: str, role: str) -> str:
    return name or role or "control"
