"""Download the Unión Personal provider directory (AMBA) to CSV.

The site's search (unionpersonal.com.ar/cartilla) is backed by a JSON API,
browsed as a tree: plan > tipo > subzona > categoría > especialidad >
localidad > prestadores. The API key is the public one shipped in the
site's JavaScript bundle. Only the "metropolitana" zone (CABA and Gran
Buenos Aires) is downloaded, which is what the geocoder covers.

The API returns one record per provider office and specialty; offices are
merged here, joining their specialties.

Usage: python3 fetch_unionpersonal.py [output.csv]
"""

import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = "https://api.unionpersonal.com.ar/cartilla/"
KEY = (
    "vL1gTwRCkOtgr6Fu1p0UBa778OnMDCpgoNlbNTURHMBIM3iYKHvLepDSkWq8E1twKwgT9NYoy5s0"
    "OSlt9eQqFFqXM8vXTNFsj7H3NWBqOfLpUuvVBqroodPsRbzpUGTVG7kWwZLEfkYe2NCxi1pBhgjP"
    "0I8ytcaagawZ7LOqEG7CGnKxJcPKLUZXtgPF5jhqgK0mhnLUaaaaQdwYhAK6eJaAHqKFgHzqCSxC"
    "Snl3ipfRGW4zBhXjP7YcAhqH2xNm"
)
OS = "up"
ZONA = "metropolitana"
WORKERS = 4


def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("�", "")).strip()


def get(*path):
    url = URL + "/".join(urllib.parse.quote(str(p), safe="") for p in path)
    req = urllib.request.Request(
        url, headers={"X-API-KEY": KEY, "User-Agent": "up-cartilla-map/1.0"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.loads(r.read().decode("utf-8"))
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(5)
    if not body["success"]:
        raise RuntimeError(f"{url}: {body['message']}")
    return body["data"]


def results(*path):
    """Children of a tree node; [] for nodes that link to an external site."""
    data = get(*path)
    if data["is_external"]:
        print(f"skipping {path}: external {data['external_url']}", file=sys.stderr)
        return []
    return data["results"]


def leaf(plan, tipo, subzona, cat, esp):
    """Providers of one specialty, across the localities of a subzona."""
    args = (OS, plan, ZONA, subzona, tipo, cat, esp)
    return [
        p
        for loc in results("localidades", *args)
        for p in results("prestadores", *args, loc["code"])
    ]


def main(out="cartilla.csv"):
    planes = {p["codigo_plan"]: clean(p["nombre_plan"]) for p in get("planes", OS)}
    leaves = []
    for plan, plan_name in planes.items():
        for tipo in get("tipos", OS, plan):
            for sz in results("subzonas", OS, plan, ZONA, tipo["code"]):
                for cat in results(
                    "categorias", OS, plan, ZONA, sz["code"], tipo["code"]
                ):
                    for esp in results(
                        "especialidades",
                        *(OS, plan, ZONA, sz["code"], tipo["code"], cat["code"]),
                    ):
                        leaves.append(
                            (plan, tipo["code"], sz["code"], cat["code"], esp["code"])
                        )
        print(f"{plan_name}: {len(leaves)} specialties so far", file=sys.stderr)

    offices = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (key, provs) in enumerate(
            zip(leaves, ex.map(lambda k: leaf(*k), leaves)), 1
        ):
            if i % 100 == 0:
                print(f"specialty {i}/{len(leaves)}", file=sys.stderr)
            plan = key[0]
            for p in provs:
                k = (plan, key[1], p["IdPrestador"], p["CodConsultorio"])
                row = offices.setdefault(
                    k,
                    {
                        "plan": planes[plan],
                        "tipo": clean(p["NomCartilla"]),
                        "nombre": clean(p["NomConsultorio"] or p["NomPrestador"]),
                        "direccion": clean(p["fullDireccion"]),
                        "localidad": clean(p["NomLocalidad"]),
                        "telefonos": clean(p["Telefono"]),
                        "horario": clean(p["HorarioAtencion"]),
                        "especialidades": [],
                    },
                )
                if (esp := clean(p["NomEspecialidad"])) not in row["especialidades"]:
                    row["especialidades"].append(esp)

    rows = sorted(offices.values(), key=lambda r: (r["plan"], r["nombre"]))
    for r in rows:
        r["especialidades"] = ",".join(sorted(r["especialidades"]))
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
