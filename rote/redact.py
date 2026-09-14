"""Redaction of regulated data before it reaches the model, a log, or disk.

Two layers, because neither is sufficient alone:
  1. Screen-aware: the surface marks values that sit next to sensitive labels (NAME, SSN, ...), so
     free-text PII that no regex can recognise (names, addresses) is still caught. See dom.js.
  2. Pattern backstop: SSNs, card/account-length digit runs, phones, emails, DOB-shaped dates.

Levels:
  "model"  what the LLM may see. Money stays visible (the model must locate the balance it is
           asked to read); identity data does not.
  "log"    what may be persisted. Money is masked too; persisted evidence never needs the value.
"""

from __future__ import annotations

import re
from typing import Any, Literal

Level = Literal["model", "log"]

_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"\b\d{3}-\d{2}-(\d{4})\b"), lambda m: f"***-**-{m.group(1)}"),
    (re.compile(r"\b\d{3}-\d{3}-(\d{4})\b"), lambda m: f"***-***-{m.group(1)}"),
    (re.compile(r"\b\d{2}/\d{2}/\d{4}\b"), "**/**/****"),
    (re.compile(r"\b\d{5,15}(\d{4})\b"), lambda m: f"****{m.group(1)}"),
    (re.compile(r"[\w.+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}\b"), "[email]"),
]
_MONEY = re.compile(r"\$\s?[\d,]+(\.\d{2})?")
SENSITIVE_MARK = "‹redacted›"  # what dom.js substitutes for label-adjacent values


def redact_text(text: str | None, level: Level = "log") -> str | None:
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    if level == "log":
        text = _MONEY.sub("$*.**", text)
    return text


def _label_key(s: str) -> str:
    return " ".join(s.split()).rstrip(":*").strip().upper()


def redact_cells(cells: list[str], labels: list[str]) -> list[str]:
    """Blank any cell whose nearest non-empty predecessor is a sensitive label (NAME | JANE Q SAMPLE)."""
    keys = {_label_key(label) for label in labels}
    out, prev = [], ""
    for c in cells:
        out.append(SENSITIVE_MARK if prev and _label_key(prev) in keys and c.strip() else c)
        if c.strip():
            prev = c
    return out


def redact_page_text(text: str | None, labels: list[str], level: Level = "log") -> str | None:
    """Label-aware + pattern redaction for rendered page text (table cells arrive tab-separated)."""
    if not text:
        return text
    keys = {_label_key(label) for label in labels}
    lines = []
    for line in text.splitlines():
        if "\t" in line:
            line = "\t".join(redact_cells(line.split("\t"), labels))
        else:
            m = re.match(r"^\s*([A-Za-z #]+?)\s*:\s*(.+)$", line)
            if m and _label_key(m.group(1)) in keys:
                line = f"{m.group(1)}: {SENSITIVE_MARK}"
        lines.append(line)
    return redact_text("\n".join(lines), level)


def redact_value(value: Any, classification: str, level: Level = "log") -> Any:
    """Redact a typed value according to its declared classification."""
    if value is None:
        return None
    if classification == "secret":
        return "[secret]"
    if classification == "pii":
        s = str(value)
        return f"[pii:{len(s)} chars]"
    if classification == "financial" and level == "log":
        return "[financial]"
    if classification == "internal" and level == "log":
        s = str(value)
        return s[:1] + "*" * max(0, len(s) - 2) + s[-1:] if len(s) > 2 else "**"
    return value


def redact_obj(obj: Any, level: Level = "log") -> Any:
    """Deep-redact free text inside a JSON-like structure (for event logs)."""
    if isinstance(obj, str):
        return redact_text(obj, level)
    if isinstance(obj, dict):
        return {k: ("[secret]" if k.lower() in {"password", "secret", "token", "api_key"} else redact_obj(v, level))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, level) for v in obj]
    return obj
