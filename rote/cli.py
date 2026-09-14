"""rote command line.

  rote serve                          run both CoreServ tenants (prairie :8601, lakeshore :8602)
  rote discover  --tenant T --goal G --capability-id ID [--console] [--headed]
  rote replay    CAP[@ver] --tenant T --input k=v ... [--inject JSON] [--console] [--approve STEP]
  rote approve   CAP@ver --by NAME
  rote catalog   --tenant T           agent-facing tool definitions for approved capabilities
  rote invoke    TOOL --tenant T --args JSON
  rote ask       "question" --tenant T    Claude answers using the catalog (replay does the work)
  rote schema                         export JSON Schemas for the artifact and result contracts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

from rote.registry import ROOT, Registry, dump_yaml
from rote.schema.capability import Capability
from rote.schema.result import ReplayResult


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------- serve
def cmd_serve(a: argparse.Namespace) -> None:
    from targetapp.coreserv import serve
    threads = []
    for tenant, port in (("prairie", 8601), ("lakeshore", 8602)):
        t = threading.Thread(target=serve, args=(tenant, port), daemon=True)
        t.start()
        threads.append(t)
        print(f"CoreServ tenant {tenant:<10} http://127.0.0.1:{port}/signon")
    print("Ctrl-C to stop.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass


def _inject(base_url: str, faults: dict[str, Any]) -> None:
    for path, body in (("/__reset", {}), ("/__faults", faults)):
        req = urllib.request.Request(base_url.rstrip("/") + path, data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()


# ---------------------------------------------------------------------------- discover
async def cmd_discover(a: argparse.Namespace) -> int:
    from rote.discovery.agent import Discoverer, DiscoveryConfig
    from rote.operator.console import Console
    from rote.runtime import open_session

    registry = Registry()
    console = Console(a.console_port) if a.console else None
    if console:
        await console.start()
    cfg = DiscoveryConfig(goal=a.goal, capability_id=a.capability_id, model=a.model, effort=a.effort,
                          max_steps=a.max_steps, escalation_timeout_s=a.escalation_timeout if console else None)
    if a.reset:
        _inject(registry.tenant(a.tenant).base_url, {})
    try:
        async with open_session("discover", a.tenant, headed=a.headed, label=a.goal[:60]) as s:
            if console:
                print(f"\n  operator console: {console.register(s)}\n", file=sys.stderr, flush=True)
            d = Discoverer(s, cfg)
            outcome = await d.run()
            summary = {"run_id": s.id, "status": outcome.status, "summary": outcome.summary,
                       "artifact": outcome.artifact_path, "steps": outcome.steps, "usage": outcome.usage,
                       "evidence": str(s.evidence.dir)}
        if outcome.capability and a.verify:
            async with open_session("verify", a.tenant, label=f"verify {outcome.capability.ref}") as vs:
                ver = await d.verify(outcome, vs)
            cap = outcome.capability.model_copy(deep=True)
            cap.provenance.verification = ver
            if ver["status"] != "succeeded":
                cap.review.notes = (cap.review.notes or "") + f" VERIFICATION FAILED ({ver['status']})."
            registry.save(cap)
            summary["verification"] = ver
        _print(summary)
        return 0 if outcome.status == "recorded" else 2
    finally:
        if console:
            await console.stop()


# ---------------------------------------------------------------------------- replay
async def replay_once(cap_ref: str, tenant_id: str, inputs: dict[str, Any], *, console_port: int | None = None,
                      headed: bool = False, allow_draft: bool = False, approvals: set[str] | None = None,
                      inject: dict | None = None, escalation_timeout: float = 900, echo: bool = True,
                      runs_dir: Path | None = None, registry: Registry | None = None) -> ReplayResult:
    from rote.operator.console import Console
    from rote.replay.engine import Replayer, ReplayOptions
    from rote.runtime import open_session

    registry = registry or Registry()
    tenant = registry.tenant(tenant_id)
    eff = registry.effective(registry.load(cap_ref), tenant)
    if inject is not None:
        _inject(tenant.base_url, inject)
    console = Console(console_port) if console_port else None
    if console:
        await console.start()
    try:
        async with open_session("replay", tenant_id, headed=headed, label=eff.capability.ref, echo=echo,
                                runs_dir=runs_dir, registry=registry) as s:
            if console:
                print(f"\n  operator console: {console.register(s)}\n", file=sys.stderr, flush=True)
            if inject:
                s.evidence.event("test.fault_injected", faults=inject)
            rep = Replayer(registry=registry, surface=s.surface, profile=s.profile, policy=s.policy, tenant=s.tenant,
                           evidence=s.evidence, control=s.control, secrets=s.secrets,
                           options=ReplayOptions(approvals=approvals or set(), allow_draft=allow_draft,
                                                 escalation="wait" if console else "return",
                                                 escalation_timeout_s=escalation_timeout))
            return await rep.run(eff.capability, inputs, eff.applied_overlays)
    finally:
        if console:
            await console.stop()


async def cmd_replay(a: argparse.Namespace) -> int:
    inputs = dict(kv.split("=", 1) for kv in a.input)
    result = await replay_once(a.capability, a.tenant, inputs, console_port=a.console_port if a.console else None,
                               headed=a.headed, allow_draft=a.allow_draft, approvals=set(a.approve),
                               inject=json.loads(a.inject) if a.inject else ({} if a.reset else None),
                               escalation_timeout=a.escalation_timeout)
    _print(result.model_dump(mode="json", exclude_none=True))
    return {"succeeded": 0, "business_outcome": 0}.get(result.status, 2)


# ---------------------------------------------------------------------------- approve
def cmd_approve(a: argparse.Namespace) -> int:
    from rote.evidence import now_iso
    registry = Registry()
    cap = registry.load(a.capability)
    ver = cap.provenance.verification or {}
    if cap.provenance.method == "discovered" and ver.get("status") != "succeeded" and not a.force:
        print(f"refusing: {cap.ref} has no successful verification replay (use --force to override)")
        return 2
    if ver.get("procedure_digest") and ver["procedure_digest"] != cap.procedure_digest() and not a.force:
        print(f"refusing: {cap.ref} steps/locators changed since verification; re-run verification")
        return 2
    if cap.provenance.human_steps and not a.force:
        print(f"refusing: {cap.ref} contains human steps; review them and pass --force")
        return 2
    cap.review.status, cap.review.approved_by, cap.review.approved_at = "approved", a.by, now_iso()
    cap.review.approved_digest = cap.procedure_digest()
    path = registry.save(cap)
    print(f"approved {cap.ref} -> {path}")
    return 0


# ---------------------------------------------------------------------------- catalog / invoke / ask
def tool_name(cap: Capability) -> str:
    return cap.id.replace(".", "_")


def catalog(registry: Registry, tenant_id: str, include_drafts: bool = False) -> list[dict[str, Any]]:
    tenant = registry.tenant(tenant_id)
    tools = []
    for cap in registry.all():
        if cap.app.product != tenant.app or cap.id == registry.profile(tenant.app).auth.capability:
            continue
        if cap.review.status != "approved" and not include_drafts:
            continue
        props, required = {}, []
        for name, spec in cap.inputs.items():
            if spec.source == "secret":
                continue
            p: dict[str, Any] = {"type": "string", "description": spec.description}
            if spec.type == "enum":
                p["enum"] = spec.values
            elif spec.pattern:
                p["pattern"] = spec.pattern
            elif spec.type == "integer":
                p["pattern"] = r"^-?\d+$"
            elif spec.type == "money":
                p["pattern"] = r"^\d+(\.\d{1,2})?$"
            props[name] = p
            if spec.required:
                required.append(name)
        outs = ", ".join(f"{k} ({v.type})" for k, v in cap.outputs.items()) or "none"
        outcomes = ", ".join(o.code for o in cap.outcomes) or "none"
        desc = (f"{cap.title}. {cap.description} Returns: {outs}. Possible business outcomes (not errors): "
                f"{outcomes}. Risk: {cap.risk}"
                + (" - irreversible steps pause for human approval." if cap.risk == "irreversible" else ".")
                + f" [{cap.ref}, runs deterministically without a model]")
        tools.append({"name": tool_name(cap), "description": desc,
                      "input_schema": {"type": "object", "properties": props, "required": required,
                                       "additionalProperties": False}})
    return tools


def cmd_catalog(a: argparse.Namespace) -> int:
    _print(catalog(Registry(), a.tenant, a.include_drafts))
    return 0


def _cap_for_tool(registry: Registry, name: str) -> Capability:
    for cap in registry.all():
        if tool_name(cap) == name:
            return cap
    raise SystemExit(f"no capability for tool {name!r}")


def _for_agent(r: ReplayResult) -> dict[str, Any]:
    """What the calling agent needs: status + outputs / outcome / failure summary. Not evidence paths."""
    out: dict[str, Any] = {"status": r.status}
    if r.outputs:
        out["outputs"] = r.outputs
    if r.outcome:
        out["outcome"] = {"code": r.outcome.code, "message": r.outcome.message}
    if r.failure:
        out["failure"] = {"code": r.failure.code, "message": r.failure.message, "step": r.failure.step_id,
                          "retryable": r.failure.retryable}
    if r.escalations:
        out["escalations"] = [{"kind": e.kind, "reason": e.reason} for e in r.escalations]
    out["run_id"] = r.run_id
    return out


async def cmd_invoke(a: argparse.Namespace) -> int:
    registry = Registry()
    cap = _cap_for_tool(registry, a.tool)
    result = await replay_once(cap.id, a.tenant, json.loads(a.args), echo=False)
    _print(_for_agent(result))
    return 0


async def cmd_ask(a: argparse.Namespace) -> int:
    import anthropic
    registry = Registry()
    tools = catalog(registry, a.tenant)
    client = anthropic.AsyncAnthropic()
    system = ("You are a credit union member-service assistant for back-office staff. Use the provided tools to "
              "look things up; they run recorded automations against the core system. Treat business outcomes "
              "(like RECORD_NOT_FOUND) as answers, not errors. Be concise.")
    messages: list[dict[str, Any]] = [{"role": "user", "content": a.question}]
    print(f"[catalog] {len(tools)} tool(s): {[t['name'] for t in tools]}")
    for _ in range(6):
        resp = await client.beta.messages.create(model=a.model, max_tokens=4000, system=system, tools=tools,
                                                 messages=messages, thinking={"type": "adaptive"},
                                                 betas=["server-side-fallback-2026-07-01"],
                                                 extra_body={"fallbacks": "default"})
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            print("\n" + "".join(b.text for b in resp.content if b.type == "text"))
            return 0
        results = []
        for tu in uses:
            cap = _cap_for_tool(registry, tu.name)
            print(f"[agent -> {tu.name}] {json.dumps(tu.input)}")
            r = await replay_once(cap.id, a.tenant, dict(tu.input), echo=False)
            payload = _for_agent(r)
            print(f"[replay <- {r.status}] run {r.run_id} in {r.duration_ms} ms")
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(payload)})
        messages.append({"role": "user", "content": results})
    return 2


# ---------------------------------------------------------------------------- schema
def cmd_schema(a: argparse.Namespace) -> int:
    from rote.schema.profile import AppProfile
    from rote.schema.tenant import Tenant
    out = ROOT / "schema"
    out.mkdir(exist_ok=True)
    for name, model in (("capability", Capability), ("replay_result", ReplayResult), ("app_profile", AppProfile),
                        ("tenant", Tenant)):
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(by_alias=True), indent=2))
        print(f"wrote schema/{name}.schema.json")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    registry = Registry()
    cap = registry.load(a.capability)
    if a.tenant:
        eff = registry.effective(cap, registry.tenant(a.tenant))
        print(f"# overlays applied: {eff.applied_overlays}")
        cap = eff.capability
    print(dump_yaml(cap))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rote", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve")

    d = sub.add_parser("discover")
    d.add_argument("--tenant", default="prairie")
    d.add_argument("--goal", required=True)
    d.add_argument("--capability-id", required=True)
    d.add_argument("--model", default="claude-opus-5")
    d.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    d.add_argument("--max-steps", type=int, default=30)
    d.add_argument("--console", action="store_true", help="serve the operator console and wait for humans")
    d.add_argument("--console-port", type=int, default=8710)
    d.add_argument("--escalation-timeout", type=float, default=900)
    d.add_argument("--headed", action="store_true")
    d.add_argument("--no-verify", dest="verify", action="store_false")
    d.add_argument("--reset", action="store_true", help="reset target app data/faults first (demo only)")

    r = sub.add_parser("replay")
    r.add_argument("capability")
    r.add_argument("--tenant", default="prairie")
    r.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--approve", action="append", default=[], metavar="STEP_ID",
                   help="pre-approve an irreversible step (stands in for a recorded human approval)")
    r.add_argument("--inject", help="demo only: JSON fault spec POSTed to the target app before the run")
    r.add_argument("--reset", action="store_true", help="demo only: reset target app data/faults first")
    r.add_argument("--console", action="store_true")
    r.add_argument("--console-port", type=int, default=8710)
    r.add_argument("--escalation-timeout", type=float, default=900)
    r.add_argument("--headed", action="store_true")

    p = sub.add_parser("approve")
    p.add_argument("capability")
    p.add_argument("--by", required=True)
    p.add_argument("--force", action="store_true")

    c = sub.add_parser("catalog")
    c.add_argument("--tenant", default="prairie")
    c.add_argument("--include-drafts", action="store_true")

    i = sub.add_parser("invoke")
    i.add_argument("tool")
    i.add_argument("--tenant", default="prairie")
    i.add_argument("--args", default="{}")

    q = sub.add_parser("ask")
    q.add_argument("question")
    q.add_argument("--tenant", default="prairie")
    q.add_argument("--model", default="claude-opus-5")

    sh = sub.add_parser("show")
    sh.add_argument("capability")
    sh.add_argument("--tenant")

    sub.add_parser("schema")

    a = ap.parse_args(argv)
    env = ROOT / ".env"
    if env.exists():  # convenience for local runs; real deployments inject env/secrets
        import os
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    handlers = {"serve": cmd_serve, "discover": cmd_discover, "replay": cmd_replay, "approve": cmd_approve,
                "catalog": cmd_catalog, "invoke": cmd_invoke, "ask": cmd_ask, "schema": cmd_schema, "show": cmd_show}
    h = handlers[a.cmd]
    rc = asyncio.run(h(a)) if asyncio.iscoroutinefunction(h) else h(a)
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
