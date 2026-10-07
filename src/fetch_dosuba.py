"""Download the full DOSUBA provider directory (cartilla.dosuba.uba.ar) to CSV.

The site's search form validates filters only client-side; the backend
endpoint accepts an empty query and returns every provider, paginated
10 per page as an HTML fragment.

Usage: python3 fetch_dosuba.py [output.csv]
"""

import csv
import html
import json
import math
import re
import sys
import time
import urllib.request

URL = "https://cartilla.dosuba.uba.ar/_ResultadoCartilla"
PLANES = {"1": "General", "2": "Estudiantes y Graduados"}
PAGE_SIZE = 10
DELAY = 0.5  # seconds between requests

CARD = re.compile(r'<div class="card border-success.*?</p>', re.DOTALL)
TOTAL = re.compile(r"Prestadores encontrados \((\d+)\)")
NAME = re.compile(r'class="card-link" href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
TEL = re.compile(r'<a href="tel:[^"]*">(.*?)</a>', re.DOTALL)


def field(label, card):
    m = re.search(rf"<b>{label}:</b>(.*?)<br", card, re.DOTALL)
    return clean(m.group(1)) if m else ""


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def fetch(plan, page):
    body = {
        "EyG": plan,
        "Especialidad": "",
        "Nombre": "",
        "Zona": "",
        "DireccionLocalidad": "",
        "Pagina": str(page),
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except OSError:
            if attempt == 2:
                raise
            time.sleep(5)


def parse(page_html):
    for card in CARD.findall(page_html):
        maps_url, name = NAME.search(card).groups()
        yield {
            "nombre": clean(name),
            "direccion": field("Dirección", card),
            "localidad": field("Localidad", card),
            "telefonos": " | ".join(clean(t) for t in TEL.findall(card) if clean(t)),
            "especialidades": field("Especialidades", card),
            "maps_url": html.unescape(maps_url),
        }


def main(out="cartilla.csv"):
    rows = []
    for plan, plan_name in PLANES.items():
        first = fetch(plan, 1)
        total = int(TOTAL.search(first).group(1))
        n_pages = math.ceil(total / PAGE_SIZE)
        plan_rows = list(parse(first))
        for page in range(2, n_pages + 1):
            time.sleep(DELAY)
            plan_rows.extend(parse(fetch(plan, page)))
            print(f"{plan_name}: page {page}/{n_pages}", file=sys.stderr)
        if len(plan_rows) != total:
            print(
                f"WARNING {plan_name}: got {len(plan_rows)}, expected {total}",
                file=sys.stderr,
            )
        rows.extend({"plan": plan_name, **r} for r in plan_rows)

    # By every column, so the order doesn't depend on the site's pagination.
    rows.sort(key=lambda r: (r["plan"], r["nombre"], *r.values()))

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
