# rote

**Record once, replay deterministically.** An LLM works out how to complete a goal inside a legacy UI
that has no API. The successful run becomes a typed, versioned, reviewable **capability**. That
capability then replays with **no model in the loop**. Runtime errors are classified, human handoff
works on the live session, and guardrails apply the whole time.

Design write-up: **[REPORT.md](REPORT.md)**. Evidence: **[evidence/](evidence/README.md)**.

```
goal ──► discovery (Claude Opus 5 drives the live UI) ──► capability YAML (draft)
                                                            │ verification replay + human review
                                                            ▼
  calling agent ──► catalog tool ──► deterministic replay ──► succeeded | business_outcome | failed | needs_human
                                          │   ▲
                                          ▼   │ take control / hand back
                                     operator console (same live session)
```

The target is **CoreServ** (`targetapp/coreserv.py`), a synthetic legacy credit-union core that is
hostile on purpose. It has framesets, table layouts, unlabeled fields, a `<td onclick>` "button",
status-line errors, interstitials, session expiry, and injectable host errors. It runs as two
tenants that are configured differently. All data is fake.

---

## Setup

Requires Python 3.11+ and macOS or Linux.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env        # add ANTHROPIC_API_KEY (discovery only)
```

| Config | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `rote discover`, `rote ask` | Replay, tests and the console never call a model. |
| `CORESERV_USER` / `CORESERV_PASSWORD` | sign-on | Demo values are in `.env.example`. They are resolved by the secret provider and never shown to the model. |

**Running without live services.** Everything except discovery and `ask` is offline. The target app is
local, replay has no model, and the test suite starts its own app servers and headless Chromium. The
committed capabilities in `capabilities/` came from real discovery runs (`evidence/01-discovery`), so
you can replay them without an API key.

## Demo path

Terminal 1, the target app (both tenants):

```bash
.venv/bin/rote serve
```

Terminal 2:

**1. Discover.** Claude drives the UI toward a goal. The recorder writes a *draft* capability, and
rote immediately replays it in a fresh browser to verify it. (A new id is used here so your run
doesn't shadow the committed, approved one.)

```bash
.venv/bin/rote discover --tenant prairie --reset \
  --capability-id keystone.coreserv.my_savings_balance \
  --goal "Look up member 12345 and read their current savings balance (the REGULAR SHARES account)."
```

**2. Replay the resulting artifact.** Drafts need `--allow-draft`, or approve first. Approval is
refused unless the verification replay succeeded against the same procedure digest.

```bash
.venv/bin/rote replay keystone.coreserv.my_savings_balance --allow-draft --input member_number=20417
.venv/bin/rote approve keystone.coreserv.my_savings_balance@1.0.0 --by you
```

**3. Replay the committed, approved capabilities through the error taxonomy.**

```bash
R=".venv/bin/rote replay keystone.coreserv.member_savings_balance"
$R --reset --input member_number=12345          # succeeded, outputs.regular_shares_balance = "2418.37"
$R --reset --input member_number=99999          # business_outcome RECORD_NOT_FOUND
$R --reset --input member_number=31008          # business_outcome PERMISSION_DENIED
$R --reset --input member_number=12A45          # invalid_request INVALID_INPUT (UI never touched)
$R --reset --input member_number=20417          # succeeded, recoveries: member_alert (dismissed)
$R --input member_number=12345 --inject '{"error_next":1,"error_path":"/mi/detail"}'   # recovered by restart
$R --input member_number=12345 --inject '{"expire_session":true,"expire_path":"/mi/detail"}'  # re-authenticates
$R --input member_number=12345 --inject '{"error_next":9,"error_path":"/mi/detail"}'   # failed RECOVERY_EXHAUSTED
$R --tenant lakeshore --reset --input member_number=12345                               # tenant B via overlay
```

`--inject` and `--reset` call the demo app's out-of-band fault endpoint. They are test tooling, not
part of the engine.

**4. Human handoff on the live session.** Inject a screen no profile knows about, then take over in
the console.

```bash
$R --console --input member_number=12345 --inject '{"unknown_modal_next":1,"unknown_modal_path":"/mi/detail"}'
# open the printed console URL -> Take control -> click DISMISS in the live view -> resume
```

Irreversible steps pause for approval on every execution:

```bash
.venv/bin/rote replay keystone.coreserv.open_sub_account --console --reset \
  --input member_number=48213 --input "product=SHARE CERTIFICATE 12 MO" --input "nickname=COLLEGE FUND" \
  --input opening_deposit=500.00 --input fund_from_suffix=0000
# console -> Take control -> approve   (or pass --approve s10_confirm_and_open to pre-approve)
```

**5. Agent-facing catalog.** Approved capabilities become tools.

```bash
.venv/bin/rote catalog --tenant prairie
.venv/bin/rote invoke keystone_coreserv_member_savings_balance --args '{"member_number":"55555"}'
.venv/bin/rote ask "What is the savings balance for member 20417, and for member 55555?"
```

A real replay result for member 20417 (whose record shows a member-alert interstitial), as the caller receives it on stdout. The persisted copy under `runs/` redacts outputs by
classification:

```json
{
  "run_id": "20260914T222301Z-replay-947ff1",
  "capability": "keystone.coreserv.member_savings_balance@1.0.0",
  "tenant": "prairie",
  "status": "succeeded",
  "outputs": { "regular_shares_balance": "15002.55" },
  "escalations": [],
  "recoveries": [
    { "condition": "member_alert", "action": "dismiss", "step_id": "s03_submit_the_member_search", "attempt": 1 }
  ],
  "warnings": [],
  "steps_executed": 4,
  "attempts": 1,
  "duration_ms": 1838,
  "evidence_dir": "runs/20260914T222301Z-replay-947ff1"
}
```

## Tests

```bash
.venv/bin/pytest            # 63 tests, about 90s, no model calls
.venv/bin/ruff check .
```

- **Unit:** schema integrity, procedure digest, policy classification and allowlist, redaction, input
  validation and money parsing, overlays, recorder synthesis, and the control-transfer state machine.
- **Integration** (real app + Chromium): every row of the replay taxonomy, approval gates, overlays and
  drift diagnosis, the structural-fallback refusal, a scripted operator handoff with re-sync, locator
  ambiguity and conflict on hostile markup, and network-level allowlist blocking.
- **Discovery loop:** a scripted stand-in model exercises policy blocks, forced parameterisation, stuck
  detection, and that a recorded artifact replays for a *different* member.
- **Leak canary:** scans every evidence file the suite wrote for seeded PII, balances and the password.

`scripts/regenerate_all.sh` rebuilds every model-produced artifact and the automated evidence from scratch.

## Layout

```
rote/
  schema/          capability.py (the artifact), profile.py, tenant.py, result.py (replay contract)
  surface/         base.py (the seam), web.py + dom.js (Playwright; locators, redaction, snapshots)
  discovery/       agent.py (Claude tool loop, stuck detection), recorder.py (trace -> capability)
  replay/          engine.py (deterministic executor), conditions.py (screen/condition probing)
  control.py       live-session control transfer; operator/console.py (bare console)
  policy.py        allowlist + risk model;  redact.py;  evidence.py;  registry.py (versions, overlays)
apps/keystone.coreserv/profile.yaml    vendor-level conditions, auth, risk hints, sensitive labels
capabilities/<id>/<version>.yaml       artifacts (2 discovered + reviewed, 2 authored)
tenants/                               prairie (base), lakeshore (overlay)
policies/default.yaml                  allowlist and risk policy
targetapp/coreserv.py                  the synthetic legacy app + fault injection
evidence/                              discovery, verification, replay matrix, handoffs, drift, catalog
```

## What is mocked, and why

| Piece | Status | Why |
|---|---|---|
| Target application | Built locally (CoreServ) | No real bank system. Local is also the only way to inject session expiry, host abends and unknown modals on demand. |
| Operator console | Bare but real: same live session, lease token, recorded actions | A co-browsing product is out of scope. The control model is what's being evaluated. |
| Operator auth, notifications | Not built | Would be SSO + roles, and a queue/webhook to an ops channel. |
| Secret store | Env / `.env` provider behind an interface | Swap for a vault client. |
| Desktop surface | Not built; seam described in REPORT §4 | One concrete surface was in scope. |
| `--approve STEP` | Stands in for a recorded human approval | Lets unattended demos exercise the commit path. The console path is the real one. |
