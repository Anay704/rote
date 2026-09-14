"""App profile: vendor-product-level knowledge shared by every capability and every tenant running
that product. Runtime conditions live here rather than in each capability because "session expired"
or "system error" look the same no matter which flow hit them."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from rote.schema.capability import Locator, PageScope, Scope, Strict


class TextMatch(Strict):
    kind: Literal["text_matches"] = "text_matches"
    scope: Scope = Field(default_factory=PageScope)
    pattern: str = Field(description="Regex searched in the visible text of the scope.")


class UrlMatch(Strict):
    kind: Literal["url_path"] = "url_path"
    scope: Scope = Field(default_factory=PageScope)
    path: str


Detector = Annotated[Union[TextMatch, UrlMatch], Field(discriminator="kind")]


class Dismiss(Strict):
    """Click a known control to clear an interstitial, then keep waiting for the same expectation."""
    kind: Literal["dismiss"] = "dismiss"
    locators: list[Locator]
    scope: Scope = Field(default_factory=PageScope)


class Restart(Strict):
    """Start the flow again from the entry screen. Only permitted while no irreversible step has run
    in the current attempt, which is what makes restarting safe."""
    kind: Literal["restart"] = "restart"
    reauthenticate: bool = False
    backoff_ms: int = 500


Recovery = Annotated[Union[Dismiss, Restart], Field(discriminator="kind")]


class Condition(Strict):
    id: str
    description: str
    detect: list[Detector] = Field(min_length=1, description="All detectors must match.")
    klass: Literal["business_outcome", "recoverable", "hard_failure"] = Field(alias="class")
    code: str
    message_group: int | None = Field(None, description="Regex group of the first text detector used as the message.")
    recovery: Recovery | None = None
    max_recoveries: int = 2

    model_config = {"extra": "forbid", "populate_by_name": True}


class RiskHints(Strict):
    irreversible_controls: list[str] = Field(default_factory=list, description="Regexes on control names.")
    safe_controls: list[str] = Field(default_factory=list, description="Regexes on control names.")


class Auth(Strict):
    capability: str
    secrets: dict[str, str] = Field(description="capability input name -> secret provider key")
    signed_out_screen: list[Detector]


class AppProfile(Strict):
    id: str
    vendor: str
    surface: Literal["web", "desktop"]
    entry_path: str
    auth: Auth
    conditions: list[Condition]
    risk: RiskHints = Field(default_factory=RiskHints)
    sensitive_labels: list[str] = Field(default_factory=list)
    max_attempts: int = 3
