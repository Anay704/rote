"""Loading and composing artifacts: app profiles, capabilities, tenants and overlays.

Resolution order for "run capability C for tenant T":
  1. load C (latest approved version unless pinned)
  2. check T's app_version against C.app.compatible_versions
  3. apply T's overlays for C whose base_versions range includes C.version
  4. validate the merged result as a Capability again (an overlay cannot produce an invalid artifact)
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rote.schema.capability import Capability
from rote.schema.profile import AppProfile
from rote.schema.tenant import Tenant

ROOT = Path(__file__).resolve().parent.parent


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) + (0,) * (3 - len(re.findall(r"\d+", v)[:3]))


def version_in_range(version: str, spec: str) -> bool:
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        m = re.fullmatch(r"(>=|<=|==|>|<)\s*([\d.]+)", part)
        if not m:
            raise ValueError(f"bad version constraint {part!r}")
        op, ref = m.groups()
        a, b = _vtuple(version), _vtuple(ref)
        ok = {">=": a >= b, "<=": a <= b, "==": a == b, ">": a > b, "<": a < b}[op]
        if not ok:
            return False
    return True


def merge_patch(base: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch. null deletes; dicts merge; everything else replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


PATCHABLE = {"targets", "screens", "inputs", "outputs"}


@dataclass
class Effective:
    capability: Capability
    applied_overlays: list[str]


class Registry:
    def __init__(self, root: Path = ROOT):
        self.root = root

    # ---------------------------------------------------------------- profiles / tenants
    def profile(self, app_id: str) -> AppProfile:
        return AppProfile.model_validate(yaml.safe_load((self.root / "apps" / app_id / "profile.yaml").read_text()))

    def tenant(self, tenant_id: str) -> Tenant:
        return Tenant.model_validate(yaml.safe_load((self.root / "tenants" / f"{tenant_id}.yaml").read_text()))

    def tenants(self) -> list[Tenant]:
        return [self.tenant(p.stem) for p in sorted((self.root / "tenants").glob("*.yaml"))]

    # ---------------------------------------------------------------- capabilities
    def capability_path(self, cap_id: str, version: str) -> Path:
        return self.root / "capabilities" / cap_id / f"{version}.yaml"

    def versions(self, cap_id: str) -> list[str]:
        d = self.root / "capabilities" / cap_id
        return sorted((p.stem for p in d.glob("*.yaml")), key=_vtuple) if d.exists() else []

    def _read(self, cap_id: str, version: str) -> Capability:
        return Capability.model_validate(yaml.safe_load(self.capability_path(cap_id, version).read_text()))

    def load(self, ref: str) -> Capability:
        """`id@version` pins; bare `id` resolves to the newest approved version, else the newest draft."""
        cap_id, _, version = ref.partition("@")
        versions = self.versions(cap_id)
        if not versions:
            raise FileNotFoundError(f"no capability {cap_id!r}")
        if version:
            return self._read(cap_id, version)
        caps = [self._read(cap_id, v) for v in reversed(versions)]
        return next((c for c in caps if c.review.status == "approved"), caps[0])

    def all(self) -> list[Capability]:
        base = self.root / "capabilities"
        return [self.load(d.name) for d in sorted(base.iterdir()) if d.is_dir() and self.versions(d.name)]

    def save(self, cap: Capability) -> Path:
        p = self.capability_path(cap.id, cap.version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dump_yaml(cap), encoding="utf-8")
        return p

    # ---------------------------------------------------------------- composition
    def effective(self, cap: Capability, tenant: Tenant) -> Effective:
        if tenant.app != cap.app.product:
            raise ValueError(f"tenant {tenant.id} runs {tenant.app}, capability targets {cap.app.product}")
        applied = []
        data = cap.model_dump(mode="json", by_alias=True)
        for ov in tenant.overlays:
            if ov.base != cap.id:
                continue
            if not version_in_range(cap.version, ov.base_versions):
                raise ValueError(f"overlay for {cap.id} covers {ov.base_versions}, capability is {cap.version}")
            bad = set(ov.patch) - PATCHABLE
            if bad:
                raise ValueError(f"overlay may only patch {sorted(PATCHABLE)}; got {sorted(bad)}")
            for section, patch in ov.patch.items():
                data[section] = merge_patch(data.get(section, {}), patch)
            applied.append(f"{tenant.id}:{cap.id} ({ov.reason})")
        merged = Capability.model_validate(data)
        if applied:
            if cap.review.approved_digest and cap.review.approved_digest != cap.procedure_digest():
                merged.review.status = "draft"  # base changed since approval; overlays can't launder that
            else:
                merged.review.approved_digest = merged.procedure_digest()
        return Effective(merged, applied)


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_presenter)
_Dumper.add_representer(tuple, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", list(v), flow_style=True))


def dump_yaml(cap: Capability) -> str:
    data = cap.model_dump(mode="json", by_alias=True, exclude_none=True)
    header = (f"# {cap.title}\n# {cap.ref} - generated artifact; review before approving.\n"
              f"# Schema: rote.capability/v1 (see schema/capability.schema.json)\n")
    return header + yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110)
