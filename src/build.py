"""Copy index.html into dist/, filling in the size of each cartilla's JSON.

The page shows the download progress with them: the Content-Length header can't be
used, as the server compresses the JSON.

Usage: python3 build.py
"""

import json
from pathlib import Path

PLACEHOLDER = "const SIZES = {};"


def build(src=Path("src/index.html"), dist=Path("dist")):
    sizes = {p.stem: p.stat().st_size for p in sorted(dist.glob("*.json"))}
    html = src.read_text(encoding="utf-8")
    assert PLACEHOLDER in html, f"{PLACEHOLDER!r} not found in {src}"
    html = html.replace(PLACEHOLDER, f"const SIZES = {json.dumps(sizes)};")
    (dist / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    build()
