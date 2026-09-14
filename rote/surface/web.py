"""Web surface on Playwright (Chromium).

Perception is a linearised, accessibility-style reading of each frame (roles, accessible names,
visual labels, table cells) plus a redacted screenshot. Action targets observed elements by ref.
Recording turns a ref into a Target with several independently verified locator strategies.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import BrowserContext, ElementHandle, Frame, Page, Route

from rote.policy import Policy
from rote.redact import redact_cells, redact_text
from rote.schema.capability import (
    CssLocator,
    Fingerprint,
    FrameScope,
    LabelLocator,
    Locator,
    PageScope,
    RoleLocator,
    Scope,
    TableCellLocator,
    Target,
    TextLocator,
)
from rote.surface.base import ElementInfo, LocatorError, Observation, Resolution, ScopeView

DOM_JS = (Path(__file__).parent / "dom.js").read_text()
ARIA_ROLES = {"link", "button", "textbox", "combobox", "listbox", "checkbox", "radio"}
FIELD_ROLES = {"textbox", "combobox", "listbox", "checkbox", "radio"}
VOLATILE = re.compile(r"^[\s$\d,./:\-]*$")  # money, numbers, dates: bad row keys


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _describe_control(primary: Locator, role: str, d: dict) -> str:
    if isinstance(primary, TableCellLocator):
        col = f"column '{primary.column}'" if primary.column else "the value cell"
        return f"{col} of the row keyed '{primary.row_key}'"
    name = d["name"] or d["label"] or d["text"]
    return f"{role} '{name}'" if name else f"{role} ({d['tag']})"


def _rationale(verified: list[Locator], rejected: list[str], role: str, d: dict) -> str:
    """Why these strategies, in this order, for this specific control. Written for the reviewer."""
    first = verified[0]
    parts = []
    if isinstance(first, RoleLocator):
        parts.append(f"Unique accessible role+name ({first.role} '{first.name}'): survives markup and layout changes "
                     "and maps to desktop accessibility APIs.")
    elif isinstance(first, LabelLocator):
        parts.append("No accessible name (table layout, no <label>/aria); identified the way an operator does, by the "
                     f"visible label '{first.label}' beside it.")
    elif isinstance(first, TextLocator):
        parts.append(f"{'Non-semantic clickable with no ARIA role' if role == 'clickable' else 'Control'}; matched by "
                     f"its visible text '{first.text}'.")
    elif isinstance(first, TableCellLocator):
        parts.append("Addressed by row key + column header rather than position, so added or reordered rows do not "
                     "break it. Row key is the first non-volatile cell (not an amount, date or number).")
    elif isinstance(first, CssLocator):
        parts.append("Only a structural CSS path was unique. Brittle: review before approval.")
    others = [v.strategy for v in verified[1:]]
    if others:
        parts.append(f"Fallbacks, each verified unique at record time: {', '.join(others)}; replay flags any use of "
                     "a fallback as degradation.")
    if rejected:
        parts.append("Rejected (not unique): " + "; ".join(rejected) + ".")
    return " ".join(parts)


class WebSurface:
    def __init__(self, context: BrowserContext, page: Page, policy: Policy, base_url: str,
                 sensitive_labels: list[str], on_blocked: Callable[[str, str], None] | None = None):
        self.context = context
        self.page = page
        self.policy = policy
        self.base_url = base_url.rstrip("/")
        self.sensitive_labels = sensitive_labels
        self._refs: dict[str, tuple[Frame, int]] = {}
        self._on_blocked = on_blocked
        self.acting = False  # true while automation is driving input; used to spot uncontrolled input
        self.log_terms: list[str] = []  # argument values classified pii/financial; masked in persisted images

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    async def launch(cls, pw, *, base_url: str, policy: Policy, sensitive_labels: list[str], headed: bool = False,
                     on_blocked: Callable[[str, str], None] | None = None) -> WebSurface:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(viewport={"width": 1100, "height": 720})
        surface: WebSurface | None = None

        async def guard(route: Route) -> None:
            ok, why = policy.allows_url(route.request.url)
            if ok:
                await route.continue_()
            else:
                if surface and surface._on_blocked:
                    surface._on_blocked(route.request.url, why)
                await route.abort("blockedbyclient")

        await context.route("**/*", guard)
        page = await context.new_page()
        surface = cls(context, page, policy, base_url, sensitive_labels, on_blocked)
        return surface

    async def close(self) -> None:
        browser = self.context.browser
        await self.context.close()
        if browser:
            await browser.close()

    async def goto(self, path: str) -> None:
        await self.page.goto(self.base_url + path, wait_until="load")
        await self.settle()

    async def settle(self, quiet_ms: int = 250) -> None:
        await asyncio.sleep(quiet_ms / 1000)
        for f in list(self.page.frames):
            try:
                await f.wait_for_load_state("load", timeout=10_000)
            except Exception:
                pass

    # ------------------------------------------------------------------ frames
    async def _lib(self, frame: Frame) -> None:
        await frame.evaluate(DOM_JS)

    def _scope_label(self, frame: Frame) -> str:
        return "page" if frame == self.page.main_frame else f'frame "{frame.name}"'

    def scope_of(self, frame: Frame) -> Scope:
        if frame == self.page.main_frame:
            return PageScope()
        return FrameScope(name=frame.name or None, url_path=_path(frame.url))

    def _frame(self, scope: Scope) -> tuple[Frame, bool]:
        if isinstance(scope, PageScope):
            return self.page.main_frame, False
        frames = self.page.frames
        if scope.name:
            for f in frames:
                if f.name == scope.name:
                    return f, False
        if scope.url_path:
            for f in frames:
                if f != self.page.main_frame and _path(f.url) == scope.url_path:
                    return f, True
        raise LocatorError("LOCATOR_NOT_FOUND", f"frame {scope.name or scope.url_path!r} not present",
                           attempts=[], candidates=[{"frame": f.name, "path": _path(f.url)} for f in frames])

    async def _content_frames(self) -> list[Frame]:
        out = []
        for f in self.page.frames:
            try:
                has_body = await f.evaluate("() => !!document.body && document.body.tagName !== 'FRAMESET'")
            except Exception:
                continue
            if has_body:
                out.append(f)
        return out

    # ------------------------------------------------------------------ perception
    async def observe(self, *, screenshot: bool = True) -> Observation:
        self._refs.clear()
        views: list[ScopeView] = []
        masks: list[tuple[float, float, float, float]] = []
        log_masks: list[tuple[float, float, float, float]] = []
        next_ref = 0
        for f in await self._content_frames():
            try:
                await self._lib(f)
                snap = await f.evaluate("([l, t]) => window.__rote.snapshot(l, t)", [self.sensitive_labels,
                                                                                    self.log_terms])
            except Exception:
                continue  # frame navigated mid-read; the next observation will catch it
            base = next_ref
            for i in range(snap["count"]):
                self._refs[f"e{base + i}"] = (f, i)
            next_ref += snap["count"]
            text = re.sub(r"(?<=[\[{])@(\d+)", lambda m, b=base: f"e{b + int(m.group(1))}", snap["text"])
            views.append(ScopeView(self._scope_label(f), _path(f.url), snap["title"], redact_text(text, "model")))
            off = await self._frame_offset(f)
            masks += [(x + off[0], y + off[1], w, h) for x, y, w, h in snap["maskRects"]]
            log_masks += [(x + off[0], y + off[1], w, h) for x, y, w, h in snap["logRects"]]
        png = evidence_png = None
        if screenshot:
            raw = await self.page.screenshot(type="png")
            png = self._mask(raw, masks)
            evidence_png = self._mask(raw, masks + log_masks)
        fp = hashlib.sha1("|".join(v.url_path + v.text for v in views).encode()).hexdigest()[:12]
        return Observation(views, png, fp, evidence_png)

    async def _frame_offset(self, f: Frame) -> tuple[float, float]:
        if f == self.page.main_frame:
            return (0.0, 0.0)
        try:
            el = await f.frame_element()
            box = await el.bounding_box()
            return (box["x"], box["y"]) if box else (0.0, 0.0)
        except Exception:
            return (0.0, 0.0)

    @staticmethod
    def _mask(raw: bytes, masks: list[tuple[float, float, float, float]]) -> bytes:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        draw = ImageDraw.Draw(img)
        for x, y, w, h in masks:
            draw.rectangle([x - 1, y - 1, x + w + 1, y + h + 1], fill=(20, 20, 20))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def screenshot(self) -> bytes:
        """Screenshot safe to persist: identity data, money and classified argument values masked."""
        masks = []
        for f in await self._content_frames():
            try:
                await self._lib(f)
                snap = await f.evaluate("([l, t]) => window.__rote.snapshot(l, t)", [self.sensitive_labels,
                                                                                    self.log_terms])
                off = await self._frame_offset(f)
                masks += [(x + off[0], y + off[1], w, h) for x, y, w, h in snap["maskRects"] + snap["logRects"]]
            except Exception:
                continue
        return self._mask(await self.page.screenshot(type="png"), masks)

    async def raw_screenshot(self) -> bytes:
        """Unredacted, for the live operator view only. Never persisted."""
        return await self.page.screenshot(type="png")

    async def dom_dump(self) -> dict[str, str]:
        out = {}
        for f in await self._content_frames():
            try:
                await self._lib(f)
                html = await f.evaluate("(l) => window.__rote.redactedHTML(l)", self.sensitive_labels)
                out[f.name or "main"] = redact_text(html, "log") or ""
            except Exception as e:  # pragma: no cover - diagnostic path
                out[f.name or "main"] = f"<!-- unavailable: {e} -->"
        return out

    async def scope_text(self, scope: Scope) -> str | None:
        frames = await self._content_frames() if isinstance(scope, PageScope) else []
        if not isinstance(scope, PageScope):
            try:
                frames = [self._frame(scope)[0]]
            except LocatorError:
                return None
        parts = []
        for f in frames:
            try:
                parts.append(await f.evaluate("() => document.body ? document.body.innerText : ''"))
            except Exception:
                continue
        return "\n".join(parts)

    async def scope_path(self, scope: Scope) -> str | None:
        try:
            return _path(self._frame(scope)[0].url)
        except LocatorError:
            return None

    async def screen_facts(self) -> list[tuple[Scope, str, str]]:
        """(scope, url path, first visible line) for each content frame: the raw material for screens."""
        out = []
        for f in await self._content_frames():
            try:
                line = await f.evaluate(
                    "() => (document.body.innerText || '').split('\\n').map(s => s.trim()).find(Boolean) || ''")
            except Exception:
                continue
            out.append((self.scope_of(f), _path(f.url), line))
        return out

    async def all_paths(self) -> dict[str, str]:
        return {self._scope_label(f): _path(f.url) for f in self.page.frames}

    # ------------------------------------------------------------------ elements
    async def _handle(self, ref: str) -> tuple[Frame, ElementHandle]:
        if ref not in self._refs:
            raise KeyError(f"unknown ref {ref!r}; refs are only valid for the latest observation")
        frame, idx = self._refs[ref]
        try:
            h = await frame.evaluate_handle("(i) => window.__rote && window.__rote.refs[i]", idx)
        except Exception as e:
            raise KeyError(f"ref {ref!r} is stale (page changed): {e}") from None
        el = h.as_element()
        if el is None:
            raise KeyError(f"ref {ref!r} is stale (page changed)")
        return frame, el

    async def _describe(self, el: ElementHandle) -> dict[str, Any]:
        return await el.evaluate("(e) => window.__rote.describe(e)")

    async def element(self, ref: str) -> ElementInfo:
        frame, el = await self._handle(ref)
        d = await self._describe(el)
        return self._info(ref, d, frame)

    def _info(self, ref: str, d: dict, frame: Frame) -> ElementInfo:
        href = urljoin(frame.url, d["href"]) if d["href"] and not d["href"].lower().startswith("javascript:") else d["href"]
        return ElementInfo(ref=ref, role=d["role"], name=d["name"], label=d["label"], text=d["text"], tag=d["tag"],
                           href=href, submit=d["submit"], form_method=d["form_method"], options=d["options"],
                           header=d["header"], bbox=tuple(d["bbox"]), scope=self._scope_label(frame))

    async def describe(self, res: Resolution) -> ElementInfo:
        el: ElementHandle = res.handle
        frame = await el.owner_frame()
        d = await self._describe(el)
        return self._info("", d, frame or self.page.main_frame)

    # ------------------------------------------------------------------ recording: harden a ref into a Target
    async def harden(self, ref: str, description: str) -> Target:
        frame, el = await self._handle(ref)
        d = await self._describe(el)
        role = d["role"]
        cands: list[Locator] = []

        if role in ("cell", "text"):
            cells: list[str] = await el.evaluate("(e) => window.__rote.rowCells(e)")
            me = d["text"]
            keys = [c for c in cells if c and c != me]
            keys.sort(key=lambda c: (bool(VOLATILE.match(c)), cells.index(c)))  # stable text first
            if d["header"]:
                for k in keys:
                    cands.append(TableCellLocator(row_key=k, column=d["header"]))
            idx = cells.index(me) if me in cells else -1
            prev = next((c for c in reversed(cells[:idx]) if c), None) if idx > 0 else None
            if prev and not VOLATILE.match(prev):
                cands.append(TableCellLocator(row_key=prev, column=None))
        else:
            if role in ARIA_ROLES and d["name"]:
                cands.append(RoleLocator(role=role, name=d["name"]))
            if role in FIELD_ROLES and d["label"]:
                cands.append(LabelLocator(role=role, label=d["label"]))
            if role in ("button", "link", "clickable") and d["text"]:
                cands.append(TextLocator(text=d["text"], role=role if role == "clickable" else None))
        css = await el.evaluate("(e) => window.__rote.cssPath(e)")
        cands.append(CssLocator(selector=css))

        verified: list[Locator] = []
        rejected: list[str] = []
        seen_table = False
        for loc in cands:
            hits = await self._resolve_in_frame(frame, loc)
            same = len(hits) == 1 and await hits[0].evaluate("(a, b) => a === b", el)
            if same:
                if isinstance(loc, TableCellLocator):
                    if seen_table:
                        continue  # one table strategy is enough; first is the most stable key
                    seen_table = True
                verified.append(loc)
            else:
                rejected.append(f"{loc.strategy}: {len(hits)} match(es)")
        if not verified:
            raise LocatorError("LOCATOR_NOT_FOUND", "no strategy uniquely identifies this element", attempts=[])
        fp = Fingerprint(role=role, name=redact_text(d["name"], "log") or "", label=d["label"],
                         text=redact_text(d["text"], "log") or "", bbox=tuple(round(v) for v in d["bbox"]))
        return Target(description=_describe_control(verified[0], role, d), scope=self.scope_of(frame),
                      locators=verified, fingerprint=fp, rationale=_rationale(verified, rejected, role, d))

    # ------------------------------------------------------------------ replay: resolve a Target
    async def _handles(self, frame: Frame, js: str, arg: Any) -> list[ElementHandle]:
        arr = await frame.evaluate_handle(js, arg)
        props = await arr.get_properties()
        out = [p.as_element() for p in props.values()]
        await arr.dispose()
        return [e for e in out if e is not None]

    async def _resolve_in_frame(self, frame: Frame, loc: Locator) -> list[ElementHandle]:
        await self._lib(frame)
        if isinstance(loc, RoleLocator):
            if loc.role not in ARIA_ROLES:
                return []
            return await frame.get_by_role(loc.role, name=loc.name, exact=True).element_handles()  # type: ignore[arg-type]
        if isinstance(loc, LabelLocator):
            return await self._handles(frame, "([r, l]) => window.__rote.resolveLabel(r, l)", [loc.role, loc.label])
        if isinstance(loc, TextLocator):
            return await self._handles(frame, "([t, r]) => window.__rote.resolveText(t, r)", [loc.text, loc.role])
        if isinstance(loc, TableCellLocator):
            return await self._handles(frame, "([k, c]) => window.__rote.resolveTableCell(k, c)",
                                       [loc.row_key, loc.column])
        if isinstance(loc, CssLocator):
            return await self._handles(frame, "(s) => window.__rote.resolveCss(s)", loc.selector)
        return []

    async def resolve(self, target: Target) -> Resolution:
        frame, frame_fallback = self._frame(target.scope)
        attempts: list[dict] = []
        chosen: tuple[int, ElementHandle] | None = None
        for i, loc in enumerate(target.locators):
            try:
                hits = await self._resolve_in_frame(frame, loc)
            except Exception as e:
                attempts.append({"strategy": loc.strategy, "matches": 0, "error": str(e)[:120]})
                continue
            attempts.append({"strategy": loc.strategy, "matches": len(hits)})
            if len(hits) == 1 and chosen is None:
                chosen = (i, hits[0])
            elif chosen is not None and len(hits) == 1 and loc.strategy != "css":
                # Two semantic strategies disagreeing means the screen is not what we recorded.
                if not await hits[0].evaluate("(a, b) => a === b", chosen[1]):
                    raise LocatorError("LOCATOR_CONFLICT",
                                       f"{target.locators[chosen[0]].strategy} and {loc.strategy} resolve to "
                                       f"different controls", attempts)
        if chosen is None:
            code = "LOCATOR_AMBIGUOUS" if any(a["matches"] > 1 for a in attempts) else "LOCATOR_NOT_FOUND"
            raise LocatorError(code, f"could not uniquely resolve: {target.description}", attempts,
                               await self._candidates(frame, target))
        i, el = chosen
        notes = [f"{a['strategy']} matched {a['matches']}" for a in attempts[:i]]
        return Resolution(el, i, target.locators[i].strategy, notes, frame_fallback)

    async def _candidates(self, frame: Frame, target: Target) -> list[dict]:
        """Nearby look-alikes to make a drift failure actionable ("label is now 'Member #:'")."""
        want = target.fingerprint.role if target.fingerprint else None
        try:
            await self._lib(frame)
            if want in ("cell", "text"):
                rows = await frame.evaluate(
                    "() => Array.from(document.querySelectorAll('tr')).map(r => Array.from(r.cells).map(c => "
                    "window.__rote.norm(c.innerText))).filter(r => r.length > 1 && r.length < 8).slice(0, 12)")
                return [{"row": [redact_text(c, "log") for c in redact_cells(r, self.sensitive_labels)]} for r in rows]
            els = await self._handles(frame, "() => window.__rote.interactive()", None)
            out = []
            for e in els[:40]:
                d = await self._describe(e)
                if want and d["role"] != want:
                    continue
                out.append({"role": d["role"], "name": redact_text(d["name"], "log"), "label": d["label"]})
            return out[:10]
        except Exception:
            return []

    # ------------------------------------------------------------------ actions
    async def _act(self, coro) -> None:
        self.acting = True
        try:
            await coro
        finally:
            await asyncio.sleep(0.05)
            self.acting = False

    async def click(self, res: Resolution) -> None:
        await self._act(res.handle.click(timeout=5000))

    async def fill(self, res: Resolution, text: str) -> None:
        await self._act(res.handle.fill(text, timeout=5000))

    async def options(self, res: Resolution) -> list[str]:
        return await res.handle.evaluate("(e) => Array.from(e.options || []).map(o => window.__rote.norm(o.text))")

    @staticmethod
    def pick_option(options: list[str], wanted: str, match: str) -> str | None:
        w = " ".join(wanted.split()).upper()
        if match == "label_prefix":
            hits = [o for o in options if o.upper() == w or o.upper().startswith(w + " ")]
        else:
            hits = [o for o in options if o.upper() == w]
        return hits[0] if len(hits) == 1 else None

    async def select(self, res: Resolution, option: str, match: str = "label") -> None:
        if match == "value":
            await self._act(res.handle.select_option(value=option, timeout=5000))
            return
        label = self.pick_option(await self.options(res), option, match)
        if label is None:
            raise LookupError(option)
        await self._act(res.handle.select_option(label=label, timeout=5000))

    async def press(self, res: Resolution, key: str) -> None:
        await self._act(res.handle.press(key, timeout=5000))

    async def read_text(self, res: Resolution) -> str:
        t = await res.handle.evaluate("(e) => window.__rote.norm(e.innerText || e.value || '')")
        return t

    async def click_point(self, x: float, y: float) -> None:
        await self.page.mouse.click(x, y)

    async def type_keys(self, text: str) -> None:
        await self.page.keyboard.type(text)

    async def press_key(self, key: str) -> None:
        await self.page.keyboard.press(key)
