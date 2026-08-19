"""Build a static, always-on demo bundle under dist/.

Copies the web UI, rewrites /static/ paths to the site root, injects the
demo-mode flag, and drops hosting redirects next to the files.

    python scripts/build_static_demo.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "firestop" / "web"
DIST = ROOT / "dist"

DEMO_FLAG = "<script>window.__FIRESTOP_DEMO__=true;</script>\n"
TEXT_SUFFIXES = {".html", ".css", ".js", ".svg", ".txt", ".toml"}


def _rewrite(text: str) -> str:
    return text.replace("/static/", "/")


def _inject_flag(html: str) -> str:
    marker = "<head>"
    if marker not in html:
        return DEMO_FLAG + html
    return html.replace(marker, marker + "\n    " + DEMO_FLAG.strip() + "\n", 1)


def main() -> int:
    if not WEB.is_dir():
        print(f"missing {WEB}", file=sys.stderr)
        return 1

    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(WEB, DIST)

    home = DIST / "home.html"
    if home.exists():
        shutil.copyfile(home, DIST / "index.html")

    for path in DIST.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        original = path.read_text(encoding="utf-8")
        updated = _rewrite(original)
        if path.suffix.lower() == ".html":
            updated = _inject_flag(updated)
        if updated != original:
            path.write_text(updated, encoding="utf-8")

    redirects = ROOT / "_redirects"
    if redirects.exists():
        shutil.copyfile(redirects, DIST / "_redirects")

    print(f"wrote {DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
