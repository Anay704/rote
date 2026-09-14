"""Tenant: one institution's instance of a vendor product, plus its overlays.

Overlays are RFC 7386-style merge patches over a base capability's *keyed* sections
(targets, screens, inputs, outputs). Lists (e.g. a target's locators) are replaced wholesale, which
keeps a patch readable: you see exactly the locators the tenant uses.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from rote.schema.capability import Strict


class Overlay(Strict):
    base: str = Field(description="capability id")
    base_versions: str = Field(description="e.g. '>=1.0.0,<2.0.0'; the overlay is refused outside it")
    reason: str
    patch: dict[str, Any] = Field(description="merge patch over {targets, screens, inputs, outputs}")


class Tenant(Strict):
    id: str
    display_name: str
    app: str
    app_version: str
    base_url: str
    overlays: list[Overlay] = Field(default_factory=list)
