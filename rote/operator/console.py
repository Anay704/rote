"""Minimal operator console: see open interventions, take control of the live session, hand it back.

Deliberately bare (the brief scopes out a real co-browsing console), but the control path is real:
input from this page is injected into the *same* Playwright page the automation was driving, and
only while the operator holds the lease token. Live frames are unredacted because the operator is
an authorised staff member acting on the record; nothing from the live view is persisted.

Not built: operator authn/z (would be SSO + role check), multi-operator assignment, notifications
(would be a queue/webhook to an ops channel), audit export.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from rote.control import ControlViolation
from rote.runtime import Session


class ClaimBody(BaseModel):
    intervention_id: str
    operator: str


class PreemptBody(BaseModel):
    operator: str


class InputBody(BaseModel):
    token: str
    kind: Literal["click", "type", "key"]
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None


class ReleaseBody(BaseModel):
    token: str
    decision: Literal["resume", "completed", "abort", "approve", "reject"]
    note: str | None = None


def build_app(sessions: dict[str, Session]) -> FastAPI:
    app = FastAPI(title="rote operator console")

    def get(sid: str) -> Session:
        if sid not in sessions:
            raise HTTPException(404, "no such session")
        return sessions[sid]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        rows = "".join(
            f"<tr><td><a href='/s/{s.id}'>{s.id}</a></td><td>{s.kind}</td><td>{s.label}</td>"
            f"<td><b>{s.control.holder.value}</b></td>"
            f"<td>{(s.control.current.kind + ': ' + s.control.current.reason) if s.control.current else ''}</td></tr>"
            for s in sessions.values())
        return (f"<html><head><title>rote operator console</title><meta http-equiv='refresh' content='2'></head>"
                f"<body style='font-family:system-ui;margin:24px'><h2>Live sessions</h2><table border=1 cellpadding=6>"
                f"<tr><th>session</th><th>kind</th><th>capability / goal</th><th>holder</th><th>intervention</th></tr>"
                f"{rows}</table></body></html>")

    @app.get("/api/s/{sid}")
    async def status(sid: str) -> dict:
        s = get(sid)
        return {**s.control.status(), "kind": s.kind, "label": s.label, "tenant": s.tenant.id}

    @app.get("/api/s/{sid}/screen.png")
    async def screen(sid: str) -> Response:
        s = get(sid)
        return Response(await s.surface.raw_screenshot(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/s/{sid}/claim")
    async def claim(sid: str, body: ClaimBody) -> dict:
        try:
            return {"token": get(sid).control.claim(body.intervention_id, body.operator)}
        except ControlViolation as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/s/{sid}/preempt")
    async def preempt(sid: str, body: PreemptBody) -> dict:
        try:
            get(sid).control.request_preempt(body.operator)
        except ControlViolation as e:
            raise HTTPException(409, str(e)) from None
        return {"ok": True, "note": "automation will yield at its next step boundary; then claim the intervention"}

    @app.post("/api/s/{sid}/input")
    async def human_input(sid: str, body: InputBody) -> dict:
        s = get(sid)
        try:
            s.control.assert_human(body.token)
        except ControlViolation as e:
            raise HTTPException(403, str(e)) from None
        if body.kind == "click" and body.x is not None and body.y is not None:
            await s.surface.click_point(body.x, body.y)
            s.control.record_human({"channel": "console", "input": "click", "x": round(body.x), "y": round(body.y)})
        elif body.kind == "type" and body.text is not None:
            await s.surface.type_keys(body.text)
            s.control.record_human({"channel": "console", "input": "type", "length": len(body.text)})
        elif body.kind == "key" and body.key in {"Enter", "Tab", "Escape", "Backspace"}:
            await s.surface.press_key(body.key)
            s.control.record_human({"channel": "console", "input": "key", "key": body.key})
        else:
            raise HTTPException(400, "bad input")
        await asyncio.sleep(0.3)
        return {"ok": True}

    @app.post("/api/s/{sid}/release")
    async def release(sid: str, body: ReleaseBody) -> dict:
        try:
            iv = get(sid).control.release(body.token, body.decision, body.note)
        except ControlViolation as e:
            raise HTTPException(409, str(e)) from None
        return {"ok": True, "intervention": iv.id, "decision": iv.decision}

    @app.get("/s/{sid}", response_class=HTMLResponse)
    async def session_page(sid: str) -> str:
        get(sid)
        return PAGE.replace("__SID__", sid)

    return app


class Console:
    def __init__(self, port: int):
        self.port = port
        self.sessions: dict[str, Session] = {}
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    def register(self, s: Session) -> str:
        self.sessions[s.id] = s
        return f"http://127.0.0.1:{self.port}/s/{s.id}"

    async def start(self) -> None:
        cfg = uvicorn.Config(build_app(self.sessions), host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            await self._task


PAGE = """<!doctype html><html><head><title>rote: session __SID__</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f4f4f2;color:#1b1b1b}
 header{padding:12px 20px;background:#1b1b1b;color:#fff;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 .pill{padding:3px 10px;border-radius:12px;font-weight:600;font-size:13px}
 .automation{background:#2d6cdf}.awaiting_human{background:#d98a00}.human{background:#1f9d55}
 main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;padding:16px 20px}
 @media (max-width:900px){main{grid-template-columns:1fr}}
 #screen{width:100%;max-width:1100px;border:1px solid #999;cursor:crosshair;background:#ddd}
 section{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:12px}
 pre{white-space:pre-wrap;font-size:12px;max-height:260px;overflow:auto;background:#f7f7f7;padding:8px}
 button{margin:3px 3px 3px 0;padding:6px 10px;cursor:pointer} input,textarea{width:100%;box-sizing:border-box;margin:4px 0}
 .muted{color:#666;font-size:12px}
</style></head><body>
<header><b>rote operator console</b><span>session __SID__</span><span id="holder" class="pill">…</span><span id="label" class="muted"></span></header>
<main><div><img id="screen" alt="live session"><div class="muted">Live view of the automation's own browser session.
Clicks on the image are sent to the page only while you hold control. Not persisted.</div></div>
<div>
 <section><b>Intervention</b><pre id="iv">none</pre>
  <input id="op" value="operator.demo" aria-label="operator id">
  <button id="claim">Take control</button> <button id="preempt">Request control (preempt)</button></section>
 <section><b>Act</b><input id="txt" placeholder="text to type into focused field" aria-label="text">
  <button id="type">Type</button><button data-k="Enter">Enter</button><button data-k="Tab">Tab</button><button data-k="Backspace">Backspace</button></section>
 <section><b>Hand back</b><textarea id="note" rows="2" placeholder="what you did / why"></textarea>
  <div id="decisions"></div></section>
</div></main>
<script>
const sid="__SID__"; let token=null, st=null, lastKey=null;
const $=id=>document.getElementById(id);
async function post(p,b){const r=await fetch(`/api/s/${sid}/${p}`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)});const j=await r.json();if(!r.ok)alert(j.detail||r.status);return j;}
async function refresh(){
  st=await (await fetch(`/api/s/${sid}`)).json();
  $('holder').textContent=st.holder; $('holder').className='pill '+st.holder; $('label').textContent=st.label+' · tenant '+st.tenant;
  const iv=st.intervention; $('iv').textContent=iv?JSON.stringify({id:iv.id,kind:iv.kind,reason:iv.reason,context:iv.context,human_actions:iv.human_actions.length},null,2):'none';
  const key=(iv?iv.id:'')+'|'+(token?'held':'');
  if(key!==lastKey){lastKey=key;$('decisions').innerHTML='';  // rebuild only when the control state changes
    if(iv&&token){for(const d of iv.allowed_decisions){const b=document.createElement('button');b.textContent=d;b.onclick=async()=>{await post('release',{token,decision:d,note:$('note').value});token=null;refresh();};$('decisions').appendChild(b);}}}
  $('screen').src=`/api/s/${sid}/screen.png?t=${Date.now()}`;
}
$('claim').onclick=async()=>{if(!st||!st.intervention)return alert('no open intervention');const j=await post('claim',{intervention_id:st.intervention.id,operator:$('op').value});token=j.token;refresh();};
$('preempt').onclick=()=>post('preempt',{operator:$('op').value});
$('screen').onclick=async(e)=>{if(!token)return alert('take control first');const r=e.target.getBoundingClientRect();const sx=e.target.naturalWidth/r.width;await post('input',{token,kind:'click',x:(e.clientX-r.left)*sx,y:(e.clientY-r.top)*sx});refresh();};
$('type').onclick=async()=>{if(token){await post('input',{token,kind:'type',text:$('txt').value});$('txt').value='';refresh();}};
document.querySelectorAll('[data-k]').forEach(b=>b.onclick=async()=>{if(token){await post('input',{token,kind:'key',key:b.dataset.k});refresh();}});
refresh(); setInterval(refresh,1500);
</script></body></html>"""
