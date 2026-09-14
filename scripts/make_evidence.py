"""Regenerate the replay evidence in evidence/ (discovery and human-handoff runs are captured separately:
discovery costs model calls and handoff needs a human at the console).

Usage: rote serve (in another terminal), then: .venv/bin/python scripts/make_evidence.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"
BAL = "keystone.coreserv.member_savings_balance"
REVIEW = "keystone.coreserv.open_sub_account_review"
COMMIT = "keystone.coreserv.open_sub_account"
SUB = ["member_number=48213", "product=SHARE CERTIFICATE 12 MO", "nickname=COLLEGE FUND", "opening_deposit=500.00",
       "fund_from_suffix=0000"]


def sub(**over: str) -> list[str]:
    d = dict(kv.split("=", 1) for kv in SUB)
    d.update(over)
    return [f"{k}={v}" for k, v in d.items()]


CASES: list[tuple[str, str, str, list[str], list[str]]] = [
    # (folder, what it shows, capability, inputs, extra args)
    ("03-replay/01-success", "happy path, typed output, checkpoint verified", BAL, ["member_number=12345"], ["--reset"]),
    ("03-replay/02-record-not-found", "business outcome, not an error", BAL, ["member_number=99999"], ["--reset"]),
    ("03-replay/03-permission-denied", "business outcome from a host security message", BAL,
     ["member_number=31008"], ["--reset"]),
    ("03-replay/04-invalid-input", "rejected by the input contract before touching the UI", BAL,
     ["member_number=12A45"], ["--reset"]),
    ("03-replay/05-host-validation", "host rejects the deposit: business outcome at the submitting step", REVIEW,
     sub(opening_deposit="99999.00"), ["--reset"]),
    ("03-replay/06-option-unavailable", "record-specific dropdown lacks the requested option", REVIEW,
     sub(fund_from_suffix="0050"), ["--reset"]),
    ("03-replay/07-interstitial-dismissed", "recoverable: known member-alert interstitial dismissed", BAL,
     ["member_number=20417"], ["--reset"]),
    ("03-replay/07b-system-notice-dismissed", "recoverable: broadcast system notice page dismissed", BAL,
     ["member_number=12345"], ["--inject", '{"notice_next":1,"notice_path":"/mi/detail"}']),
    ("03-replay/08-transient-error-recovered", "recoverable: host abend page, restart from entry (read-only flow)", BAL,
     ["member_number=12345"], ["--inject", '{"error_next":1,"error_path":"/mi/detail"}']),
    ("03-replay/09-session-expired-reauth", "recoverable: session expiry, re-authenticate via authored sign-on", BAL,
     ["member_number=12345"], ["--inject", '{"expire_session":true,"expire_path":"/mi/detail"}']),
    ("03-replay/10-slow-load", "3s host latency absorbed by state-based waiting", BAL, ["member_number=12345"],
     ["--inject", '{"latency_next":1,"latency_ms":3000,"latency_path":"/mi/detail"}']),
    ("03-replay/11-error-exhausted-hard-failure", "hard failure: condition persists, screenshot + DOM captured", BAL,
     ["member_number=12345"], ["--inject", '{"error_next":9,"error_path":"/mi/detail"}']),
    ("03-replay/12-unknown-state-unattended", "no profile condition explains the screen: needs_human (unattended)", BAL,
     ["member_number=12345"], ["--inject", '{"unknown_modal_next":1,"unknown_modal_path":"/mi/detail"}']),
    ("03-replay/13-irreversible-needs-approval", "irreversible step pauses for approval (unattended caller)", COMMIT,
     SUB, ["--reset"]),
    ("03-replay/14-irreversible-pre-approved", "approval supplied: commit executes, confirmation extracted", COMMIT,
     SUB, ["--reset", "--approve", "s10_confirm_and_open"]),
    ("03-replay/15-restart-refused-after-commit", "host error after the commit: restart refused (RECOVERY_UNSAFE)",
     COMMIT, SUB, ["--inject", '{"error_next":1,"error_path":"/sa/commit"}', "--approve", "s10_confirm_and_open"]),
    ("05-cross-tenant/03-lakeshore-with-overlay", "same artifact on tenant B via overlay", BAL, ["member_number=12345"],
     ["--reset", "--tenant", "lakeshore"]),
    ("05-cross-tenant/04-lakeshore-overlay-plus-profile-recovery", "tenant B still gets vendor-level recoveries", BAL,
     ["member_number=20417"], ["--reset", "--tenant", "lakeshore"]),
]


def run_case(folder: str, what: str, cap: str, inputs: list[str], extra: list[str]) -> dict:
    cmd = [str(ROOT / ".venv/bin/rote"), "replay", cap, *extra]
    for kv in inputs:
        cmd += ["--input", kv]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    result = json.loads(proc.stdout)
    dest = EVIDENCE / folder
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ROOT / "runs" / result["run_id"], dest)
    (dest / "COMMAND.txt").write_text("rote replay " + " ".join(
        f"'{a}'" if " " in a or "{" in a else a for a in cmd[2:]) + "\n")
    row = {"case": folder, "shows": what, "status": result["status"],
           "code": (result.get("outcome") or {}).get("code") or (result.get("failure") or {}).get("code") or "",
           "recoveries": ", ".join(r["condition"] for r in result.get("recoveries", [])),
           "warnings": ", ".join(sorted({w["code"] for w in result.get("warnings", [])})),
           "escalations": ", ".join(e["kind"] for e in result.get("escalations", [])),
           "attempts": result.get("attempts"), "ms": result.get("duration_ms")}
    print(f"{folder:<55} {row['status']:<17} {row['code']}", file=sys.stderr)
    return row


DRIFT_CASES = [
    ("05-cross-tenant/01-lakeshore-no-overlay", "base artifact on tenant B: structural drift diagnosed, not guessed",
     BAL, ["member_number=12345"], ["--reset", "--tenant", "lakeshore_bare"]),
    ("05-cross-tenant/02-lakeshore-frame-overlay-only", "navigation degrades to fallbacks with warnings; balance read "
     "refuses a structural fallback", BAL, ["member_number=12345"], ["--reset", "--tenant", "lakeshore_frame_only"]),
]


def drift_tenants() -> list[Path]:
    """Temporary variants of tenants/lakeshore.yaml with less overlay, to show what drift looks like."""
    import yaml
    base = yaml.safe_load((ROOT / "tenants" / "lakeshore.yaml").read_text())
    bare = {**base, "id": "lakeshore_bare", "overlays": []}
    ov = base["overlays"][0]
    frame_only = {**base, "id": "lakeshore_frame_only", "overlays": [{**ov, "patch": {
        "screens": ov["patch"]["screens"],
        "targets": {"member_inquiry_link": ov["patch"]["targets"]["member_inquiry_link"]}}}]}
    paths = []
    for t in (bare, frame_only):
        p = ROOT / "tenants" / f"{t['id']}.yaml"
        p.write_text(yaml.safe_dump(t, sort_keys=False))
        paths.append(p)
    return paths


def copy_discovery() -> None:
    """Copy the discovery + verification runs each committed artifact points to (provenance is the index)."""
    sys.path.insert(0, str(ROOT))
    from rote.registry import Registry
    for n, cap_id in enumerate((BAL, REVIEW), 1):
        cap = Registry(ROOT).load(cap_id)
        name = cap_id.split(".")[-1].replace("_", "-")
        for folder, run_id in (("01-discovery", cap.provenance.run_id),
                               ("02-verification", (cap.provenance.verification or {}).get("run_id"))):
            dest = EVIDENCE / folder / f"{n:02d}-{name}"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(ROOT / "runs" / run_id, dest)
        print(f"copied discovery + verification for {cap.ref}", file=sys.stderr)


def main() -> None:
    only = sys.argv[1:]
    if not only or "discovery" in only:
        copy_discovery()
        only = [o for o in only if o != "discovery"]
    temp = drift_tenants()
    try:
        cases = DRIFT_CASES + CASES
        rows = [run_case(*c) for c in cases if not only or any(o in c[0] for o in only)]
    finally:
        for p in temp:
            p.unlink()
    table = ["| case | shows | status | outcome / failure | recoveries | warnings | escalations | attempts | ms |",
             "|---|---|---|---|---|---|---|---|---|"]
    table += [f"| `{r['case']}` | {r['shows']} | **{r['status']}** | {r['code']} | {r['recoveries']} | "
              f"{r['warnings']} | {r['escalations']} | {r['attempts']} | {r['ms']} |" for r in rows]
    (EVIDENCE / "REPLAY_MATRIX.md").write_text("# Replay matrix (generated by scripts/make_evidence.py)\n\n"
                                               + "\n".join(table) + "\n")


if __name__ == "__main__":
    main()
