"""Wires one live session: tenant + profile + policy + surface + evidence + control channel.

Single-process by design (see REPORT.md section 1): the browser session, the automation that drives
it and the operator console that can take it over share one asyncio loop, so control transfer is an
in-memory state change rather than a distributed lock. The seam for splitting them later is
`ControlChannel` + the surface's input methods, which are all the console touches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

from rote.control import ControlChannel, Holder
from rote.evidence import Evidence, new_run_id
from rote.policy import Policy
from rote.registry import ROOT, Registry
from rote.schema.profile import AppProfile
from rote.schema.tenant import Tenant
from rote.surface.web import WebSurface
from rote.values import SecretProvider

# Captures what a human does in the live page, in any frame, whether they act through the operator
# console or directly in a headed browser window. Values are never sent, only their length.
HUMAN_CAPTURE_JS = """
(() => {
  if (window.__roteCapture) return; window.__roteCapture = true;
  const d = (el) => {
    if (!el || !el.tagName) return {};
    const t = (el.getAttribute && (el.getAttribute('type') || '')).toLowerCase();
    const text = (el.tagName === 'INPUT' && ['submit','button'].includes(t)) ? el.value : (el.innerText || '');
    return {tag: el.tagName.toLowerCase(), type: t || undefined, field: el.getAttribute('name') || undefined,
            text: text.replace(/\\s+/g, ' ').trim().slice(0, 60)};
  };
  const send = (p) => { try { window.__roteHumanEvent(Object.assign({path: location.pathname}, p)); } catch (e) {} };
  document.addEventListener('click', (e) => send({dom_event: 'click', target: d(e.target)}), true);
  document.addEventListener('change', (e) => {
    const el = e.target, sel = el.tagName === 'SELECT' ? (el.options[el.selectedIndex] || {}).text : undefined;
    send({dom_event: 'change', target: d(el), value_length: (el.value || '').length, selected: sel});
  }, true);
})();
"""


@dataclass
class Session:
    id: str
    kind: str
    registry: Registry
    tenant: Tenant
    profile: AppProfile
    policy: Policy
    surface: WebSurface
    evidence: Evidence
    control: ControlChannel
    secrets: SecretProvider
    label: str = ""


@asynccontextmanager
async def open_session(kind: str, tenant_id: str, *, runs_dir: Path | None = None, headed: bool = False,
                       label: str = "", echo: bool = True, registry: Registry | None = None,
                       base_url: str | None = None) -> AsyncIterator[Session]:
    registry = registry or Registry()
    tenant = registry.tenant(tenant_id)
    if base_url:
        tenant = tenant.model_copy(update={"base_url": base_url})
    profile = registry.profile(tenant.app)
    policy = Policy.load(registry.root / "policies" / "default.yaml", hints=profile.risk,
                         extra_origins=[tenant.base_url.rstrip("/")])
    run_id = new_run_id(kind)
    evidence = Evidence(runs_dir or (ROOT / "runs"), run_id, echo=echo)
    control = ControlChannel(run_id, evidence)

    def on_blocked(url: str, why: str) -> None:
        evidence.event("policy.network_blocked", url=url, reason=why)

    async with async_playwright() as pw:
        surface = await WebSurface.launch(pw, base_url=tenant.base_url, policy=policy,
                                          sensitive_labels=profile.sensitive_labels, headed=headed,
                                          on_blocked=on_blocked)

        async def human_event(source, payload: dict) -> None:
            if control.holder is Holder.HUMAN:
                control.record_human({"channel": "dom", **payload})
            elif not surface.acting:
                evidence.event("control.uncontrolled_input", holder=control.holder.value, **payload)

        await surface.context.expose_binding("__roteHumanEvent", human_event)
        await surface.context.add_init_script(HUMAN_CAPTURE_JS)
        session = Session(run_id, kind, registry, tenant, profile, policy, surface, evidence, control,
                          SecretProvider(registry.root / ".env"), label)
        evidence.event("session.open", kind=kind, tenant=tenant.id, app=profile.id, app_version=tenant.app_version,
                       base_url=tenant.base_url, headed=headed)
        try:
            yield session
        finally:
            evidence.event("session.close")
            await surface.close()
            evidence.close()
