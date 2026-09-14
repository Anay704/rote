"""Typed input validation and output parsing (the edges of the capability contract)."""

from __future__ import annotations

import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from rote.schema.capability import Capability, InputSpec, OutputSpec

_INT = re.compile(r"-?\d+")
_MONEY_IN = re.compile(r"\d+(\.\d{1,2})?")


def validate_inputs(cap: Capability, inputs: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for name in inputs:
        spec = cap.inputs.get(name)
        if spec is None:
            problems.append({"input": name, "problem": "unknown input"})
        elif spec.source == "secret":
            problems.append({"input": name, "problem": "secret inputs are resolved by the engine, never passed"})
    for name, spec in cap.inputs.items():
        if spec.source == "secret":
            continue
        if name not in inputs or inputs[name] in (None, ""):
            if spec.required:
                problems.append({"input": name, "problem": "required"})
            continue
        err = _check(spec, str(inputs[name]))
        if err:
            problems.append({"input": name, "problem": err})
    return problems


def _check(spec: InputSpec, raw: str) -> str | None:
    if spec.type == "integer" and not _INT.fullmatch(raw):
        return "expected an integer"
    if spec.type == "money" and not _MONEY_IN.fullmatch(raw):
        return "expected a decimal amount like 500 or 500.00"
    if spec.type == "enum" and raw not in (spec.values or []):
        return f"expected one of {spec.values}"
    if spec.type == "date":
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return "expected YYYY-MM-DD"
    if spec.pattern and not re.fullmatch(spec.pattern, raw):
        return f"does not match pattern {spec.pattern}"
    return None


def parse_output(spec: OutputSpec, text: str) -> Any:
    t = " ".join(text.split())
    if spec.type == "string":
        if not t:
            raise ValueError("empty text")
        return t
    if spec.type == "integer":
        m = re.search(r"-?[\d,]+", t)
        if not m:
            raise ValueError(f"no integer in {t!r}")
        return int(m.group(0).replace(",", ""))
    if spec.type == "money":
        m = re.fullmatch(r"\(?-?\$?\s?([\d,]+\.\d{2})\)?(\s?CR|\s?DR)?", t)
        if not m:
            raise ValueError(f"not a money amount: {t!r}")
        neg = t.startswith("(") or t.startswith("-") or (m.group(2) or "").strip() == "DR"
        try:
            amt = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation as e:
            raise ValueError(str(e)) from None
        return str(-amt if neg else amt)
    if spec.type == "date":
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y"):
            try:
                return datetime.strptime(t, fmt).date().isoformat()
            except ValueError:
                continue
        raise ValueError(f"not a date: {t!r}")
    raise ValueError(f"unsupported type {spec.type}")


class SecretProvider:
    """Resolves secrets from the environment (and a git-ignored .env). A real deployment swaps this
    for a vault client; the engine only ever asks for a key by name."""

    def __init__(self, env_file: Path | None = None):
        self._file: dict[str, str] = {}
        if env_file and env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    self._file[k.strip()] = v.strip().strip("'\"")

    def get(self, key: str) -> str | None:
        return os.environ.get(key) or self._file.get(key)
