"""The seam between "how we perceive and act on a surface" and "the recorded flow".

Everything above this interface (agent loop, recorder, replay engine, conditions, handoff) speaks
in Targets, Scopes and Observations. Everything below it is surface-specific. A desktop surface
(UIA on Windows, AX on macOS) or a pure screenshot+OCR surface implements the same protocol; see
REPORT.md section 4 for what changes and what doesn't.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from rote.schema.capability import Scope, Target


@dataclass
class ElementInfo:
    ref: str
    role: str
    name: str = ""
    label: str = ""
    text: str = ""
    tag: str = ""
    href: str | None = None
    submit: bool = False
    form_method: str | None = None
    options: list[str] | None = None
    header: str = ""
    bbox: tuple[float, float, float, float] | None = None
    scope: str = ""  # human-readable scope, e.g. frame "content"

    @property
    def control_name(self) -> str:
        """The name a human would use for this control (fields: their label, not their contents)."""
        if self.role in ("textbox", "combobox", "listbox", "checkbox", "radio"):
            return self.name or self.label
        return self.name or self.text


@dataclass
class ScopeView:
    scope: str
    url_path: str
    title: str
    text: str  # model-level redacted, linearised with @refs


@dataclass
class Observation:
    views: list[ScopeView]
    screenshot_png: bytes | None  # what the model sees
    fingerprint: str  # hash of scope paths + text, used for no-progress detection
    evidence_png: bytes | None = None  # what may be persisted: additionally masks money and classified values

    def render(self) -> str:
        return "\n\n".join(f"=== {v.scope} | path={v.url_path} ===\n{v.text}" for v in self.views)


@dataclass
class Resolution:
    handle: Any
    strategy_index: int
    strategy: str
    notes: list[str] = field(default_factory=list)  # degradation details
    frame_fallback: bool = False


class LocatorError(Exception):
    def __init__(self, code: str, message: str, attempts: list[dict], candidates: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.attempts = attempts
        self.candidates = candidates or []


class Surface(Protocol):
    async def observe(self, *, screenshot: bool = True) -> Observation: ...
    async def element(self, ref: str) -> ElementInfo: ...
    async def harden(self, ref: str, description: str) -> Target: ...
    async def resolve(self, target: Target) -> Resolution: ...
    async def click(self, res: Resolution) -> None: ...
    async def fill(self, res: Resolution, text: str) -> None: ...
    async def select(self, res: Resolution, option: str, match: str = "label") -> None: ...
    async def options(self, res: Resolution) -> list[str]: ...
    async def press(self, res: Resolution, key: str) -> None: ...
    async def read_text(self, res: Resolution) -> str: ...
    async def describe(self, res: Resolution) -> ElementInfo: ...
    async def scope_text(self, scope: Scope) -> str | None: ...
    async def scope_path(self, scope: Scope) -> str | None: ...
    async def goto(self, path: str) -> None: ...
    async def settle(self) -> None: ...
    async def screenshot(self) -> bytes: ...
    async def dom_dump(self) -> dict[str, str]: ...
