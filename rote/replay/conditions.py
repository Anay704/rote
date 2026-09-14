"""State probing: which screen are we on, and is a known runtime condition showing?

Replay never sleeps for a fixed time. It polls a cheap probe until one of three things is true:
the expected screen holds, a profile condition matches, or the deadline passes. Conditions are
checked before the screen because an error banner can share a screen with the form it rejected.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Literal

from rote.schema.capability import Capability, PageScope, Scope, TargetRule, TextRule, UrlPathRule
from rote.schema.profile import Condition, Detector, TextMatch, UrlMatch
from rote.surface.base import LocatorError, Surface


def _scope_key(scope: Scope) -> str:
    return "page" if isinstance(scope, PageScope) else f"frame:{scope.name}:{scope.url_path}"


class Probe:
    """One consistent-ish read of the surface; memoises per scope within a poll tick."""

    def __init__(self, surface: Surface):
        self.surface = surface
        self._text: dict[str, str | None] = {}
        self._path: dict[str, str | None] = {}

    async def text(self, scope: Scope) -> str | None:
        k = _scope_key(scope)
        if k not in self._text:
            self._text[k] = await self.surface.scope_text(scope)
        return self._text[k]

    async def path(self, scope: Scope) -> str | None:
        k = _scope_key(scope)
        if k not in self._path:
            self._path[k] = await self.surface.scope_path(scope)
        return self._path[k]


@dataclass
class ConditionMatch:
    condition: Condition
    message: str


async def _detector(probe: Probe, d: Detector) -> tuple[bool, str]:
    if isinstance(d, TextMatch):
        text = await probe.text(d.scope)
        if not text:
            return False, ""
        m = re.search(d.pattern, text, re.M)
        return (True, m.group(0) if not m.groups() else m.group(1)) if m else (False, "")
    if isinstance(d, UrlMatch):
        return (await probe.path(d.scope)) == d.path, ""
    return False, ""


async def match_condition(probe: Probe, conditions: list[Condition]) -> ConditionMatch | None:
    for c in conditions:
        msgs = []
        for d in c.detect:
            ok, msg = await _detector(probe, d)
            if not ok:
                break
            msgs.append(msg)
        else:
            text_msgs = [m for m in msgs if m]
            return ConditionMatch(c, text_msgs[0] if text_msgs else c.description)
    return None


async def detectors_hold(probe: Probe, detectors: list[Detector]) -> bool:
    for d in detectors:
        if not (await _detector(probe, d))[0]:
            return False
    return True


def _norm(s: str) -> str:
    return " ".join(s.split()).upper()


async def screen_holds(probe: Probe, cap: Capability, screen_id: str) -> bool:
    for rule in cap.screens[screen_id].identify:
        if isinstance(rule, UrlPathRule):
            if await probe.path(rule.scope) != rule.path:
                return False
        elif isinstance(rule, TextRule):
            text = await probe.text(rule.scope)
            if not text or _norm(rule.text) not in _norm(text):
                return False
        elif isinstance(rule, TargetRule):
            try:
                await probe.surface.resolve(cap.targets[rule.target])
            except LocatorError:
                return False
    return True


async def explain_screen(probe: Probe, cap: Capability, screen_id: str) -> list[dict]:
    """Per-rule diagnosis of why a screen does or doesn't hold. `scope_missing` marks structural drift."""
    out = []
    for rule in cap.screens[screen_id].identify:
        if isinstance(rule, UrlPathRule):
            actual = await probe.path(rule.scope)
            out.append({"rule": f"url_path {rule.path} in {_scope_key(rule.scope)}", "ok": actual == rule.path,
                        "actual": actual, "scope_missing": actual is None})
        elif isinstance(rule, TextRule):
            text = await probe.text(rule.scope)
            out.append({"rule": f"text '{rule.text}' in {_scope_key(rule.scope)}",
                        "ok": bool(text) and _norm(rule.text) in _norm(text or ""), "scope_missing": text is None})
        elif isinstance(rule, TargetRule):
            try:
                await probe.surface.resolve(cap.targets[rule.target])
                out.append({"rule": f"target {rule.target}", "ok": True, "scope_missing": False})
            except LocatorError as e:
                out.append({"rule": f"target {rule.target}", "ok": False, "actual": e.code, "scope_missing": False})
    return out


WaitKind = Literal["screen", "condition", "timeout"]


async def wait_for_screen(surface: Surface, cap: Capability, screen_id: str, conditions: list[Condition],
                          timeout_ms: int, poll_ms: int = 150) -> tuple[WaitKind, ConditionMatch | None]:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        probe = Probe(surface)
        try:
            cm = await match_condition(probe, conditions)
            if cm:
                return "condition", cm
            if await screen_holds(probe, cap, screen_id):
                return "screen", None
        except Exception:
            pass  # a frame navigated mid-probe; try again next tick
        if time.monotonic() >= deadline:
            return "timeout", None
        await asyncio.sleep(poll_ms / 1000)
