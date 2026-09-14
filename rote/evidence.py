"""Run evidence: a structured JSONL event log plus redacted screenshots and DOM dumps.

Every event passes through log-level redaction before it touches disk, so a careless call site
cannot persist a raw SSN or balance. Screenshots arriving here are already masked by the surface.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rote.redact import redact_obj


def new_run_id(kind: str) -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{kind}-{uuid.uuid4().hex[:6]}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Evidence:
    def __init__(self, root: Path, run_id: str, echo: bool = False):
        self.run_id = run_id
        self.dir = root / run_id
        (self.dir / "screens").mkdir(parents=True, exist_ok=True)
        self._log = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._t0 = time.monotonic()
        self._shot = 0
        self.echo = echo

    def event(self, event_type: str, /, **data: Any) -> None:
        rec = {**redact_obj(data), "t": round(time.monotonic() - self._t0, 3), "at": now_iso(), "event": event_type}
        self._log.write(json.dumps(rec, default=str) + "\n")
        self._log.flush()
        if self.echo:
            brief = {k: v for k, v in rec.items() if k not in ("at", "observation")}
            print(f"  · {json.dumps(brief, default=str)[:220]}", file=sys.stderr)

    def screenshot(self, png: bytes | None, label: str) -> str | None:
        if not png:
            return None
        self._shot += 1
        name = f"screens/{self._shot:03d}-{label}.png"
        (self.dir / name).write_bytes(png)
        return name

    def dom(self, dump: dict[str, str], label: str) -> str:
        d = self.dir / "dom" / label
        d.mkdir(parents=True, exist_ok=True)
        for frame, html in dump.items():
            (d / f"{frame}.html").write_text(html, encoding="utf-8")
        return str(d.relative_to(self.dir))

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        return p

    @property
    def rel_dir(self) -> str:
        """Run directory relative to the repo when possible (no local absolute paths in persisted records)."""
        from rote.registry import ROOT
        try:
            return str(self.dir.relative_to(ROOT))
        except ValueError:
            return self.dir.name

    def close(self) -> None:
        self._log.close()
