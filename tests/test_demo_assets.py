"""The hosted-demo JSON captures stay parseable and match the API shapes."""

from __future__ import annotations

import json
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "firestop" / "web"
DEMO = WEB / "demo"

REQUIRED = {
    "health.json": ("ready", "vertices"),
    "overview.json": ("services", "packages", "advisories"),
    "blast.json": ("compromise", "services", "exposed", "paths_live", "elapsed_ms"),
    "fix.json": ("changes", "paths", "severed", "followable"),
    "pivot.json": ("package", "maintainers", "siblings", "reach"),
}


def test_every_capture_is_present_and_shaped():
    for name, keys in REQUIRED.items():
        payload = json.loads((DEMO / name).read_text(encoding="utf-8"))
        missing = [key for key in keys if key not in payload]
        assert not missing, f"{name} missing {missing}"


def test_blast_is_the_lodash_incident():
    blast = json.loads((DEMO / "blast.json").read_text(encoding="utf-8"))
    assert "lodash" in blast["compromise"]["packages"]
    assert blast["exposed"] >= 1
    assert blast["services"]
    chain = blast["services"][0]["paths"][0]["chain"]
    assert chain, "path crumbs are what the console renders"


def test_seal_names_a_version_to_move_to():
    plan = json.loads((DEMO / "fix.json").read_text(encoding="utf-8"))
    assert plan["changes"]
    assert all("to_version" in change for change in plan["changes"])


def test_demo_helper_is_shipped_next_to_the_ui():
    assert (WEB / "demo.js").is_file()
