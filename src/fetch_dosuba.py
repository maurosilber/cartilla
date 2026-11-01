"""Download the full DOSUBA provider directory (cartilla.dosuba.uba.ar) to CSV.

The site's search form validates filters (and its reCAPTCHA) only
client-side; the backend endpoint accepts an empty query and returns every
provider, paginated as an HTML fragment. There is no JSON API for the results.
The server answers HTTP 429 after about 10 requests a minute.

Usage: python3 fetch_dosuba.py [output.csv]
"""

import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request

URL = "https://cartilla.dosuba.uba.ar/_ResultadoCartilla"
PLANES = {"1": "General", "2": "Estudiantes y Graduados"}
DELAY = 6  # seconds between requests, to stay under the rate limit
RATE_LIMIT_WAIT = 60  # seconds to wait after an HTTP 429

CARD = re.compile(r'<div class="card border-light.*?</p>', re.DOTALL)
TOTAL = re.compile(r"Prestadores encontrados \((\d+)\)")
LAST_PAGE = re.compile(r"Busqueda\((\d+),")
NAME = re.compile(
    r'<a [^>]*href="(https://www\.google\.com/maps[^"]*)"[^>]*>(.*?)</a>', re.DOTALL
)
TEL = re.compile(r'<a href="tel:[^"]*"[^>]*>(.*?)</a>', re.DOTALL)


def field(label, card):
    m = re.search(rf"<b>{label}:</b>(.*?)(?:<br|</p>)", card, re.DOTALL)
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
        headers={
            "Content-Type": "application/json",
            "User-Agent": "cartilla-fetch/1.0",
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except OSError as e:
            if attempt == 4:
                raise
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                print(f"Rate limited, waiting {RATE_LIMIT_WAIT}s", file=sys.stderr)
                time.sleep(RATE_LIMIT_WAIT)
            else:
                time.sleep(5)


def parse(page_html):
    for card in CARD.findall(page_html):
        maps_url, name = NAME.search(card).groups()
        yield {
            "nombre": clean(name).removeprefix("📍").strip(),
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
        # The page size isn't part of the response, so take the last page
        # from the pagination links (absent when there's a single page).
        n_pages = max((int(n) for n in LAST_PAGE.findall(first)), default=1)
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

    if not rows:
        raise RuntimeError("DOSUBA parser returned no rows")

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
