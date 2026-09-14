"""Shared fixtures. Integration tests run the real CoreServ app (both tenants) on free ports and a real
headless Chromium; nothing is mocked below the surface. No model is ever called by the test suite."""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from rote.registry import ROOT, Registry

os.environ.setdefault("CORESERV_USER", "teller01")
os.environ.setdefault("CORESERV_PASSWORD", "demo-only-pw")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def apps() -> dict[str, str]:
    from werkzeug.serving import make_server

    from targetapp.coreserv import create_app

    urls = {}
    for tenant in ("prairie", "lakeshore"):
        port = _free_port()
        srv = make_server("127.0.0.1", port, create_app(tenant), threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        urls[tenant] = f"http://127.0.0.1:{port}"
    for url in urls.values():
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/signon", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
    return urls


@pytest.fixture(scope="session")
def registry(apps, tmp_path_factory) -> Registry:
    """A throwaway copy of the artifact tree whose tenants point at the test servers."""
    root = tmp_path_factory.mktemp("rote")
    for d in ("apps", "capabilities", "policies", "tenants"):
        shutil.copytree(ROOT / d, root / d)
    for tenant, url in apps.items():
        p = root / "tenants" / f"{tenant}.yaml"
        data = yaml.safe_load(p.read_text())
        data["base_url"] = url
        p.write_text(yaml.safe_dump(data, sort_keys=False))
    return Registry(root)


@pytest.fixture(scope="session")
def runs_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("runs")
