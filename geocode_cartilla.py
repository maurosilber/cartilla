"""Geocode cartilla.csv into cartilla.js (loaded by index.html).

Tries the USIG normalizer (GCBA, covers CABA and AMBA) first, picking the
candidate whose locality matches; falls back to OpenStreetMap Nominatim.
Results are cached in geocode_cache.json so reruns only query new addresses.

Usage: python3 geocode_cartilla.py
"""

import csv
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

USIG = "https://servicios.usig.buenosaires.gob.ar/normalizar/?{}"
NOMINATIM = "https://nominatim.openstreetmap.org/search?{}"
CACHE = Path("geocode_cache.json")
UA = "dosuba-cartilla-map/1.0"
AMBA = "-59.4,-34.0,-57.7,-35.3"  # lon_min,lat_max,lon_max,lat_min


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def street(direccion):
    """'Juncal 2951 - 1 A' -> 'Juncal 2951' (drop floor/unit details)."""
    s = re.split(r"\s+-\s+|,", direccion)[0]
    m = re.match(r"(.*\d)", s)
    return (m.group(1) if m else s).strip()


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def usig(addr, localidad):
    q = urllib.parse.urlencode(
        {"direccion": addr, "geocodificar": "TRUE", "srid": 4326, "maxOptions": 20}
    )
    cands = get(USIG.format(q)).get("direccionesNormalizadas", [])
    cands = [c for c in cands if c.get("coordenadas") and c["tipo"] == "calle_altura"]
    loc = norm(localidad)
    for c in cands:
        if loc == "capital federal":
            ok = c["cod_partido"] == "caba"
        else:
            ok = loc in (norm(c["nombre_localidad"]), norm(c["nombre_partido"]))
        if ok:
            xy = c["coordenadas"]
            return float(xy["y"]), float(xy["x"]), "usig"
    return None


def nominatim(addr, localidad):
    loc = (
        "Ciudad Autónoma de Buenos Aires"
        if localidad == "Capital Federal"
        else localidad
    )
    q = urllib.parse.urlencode(
        {
            "q": f"{addr}, {loc}, Argentina",
            "format": "json",
            "limit": 1,
            "countrycodes": "ar",
            "viewbox": AMBA,  # avoid same-named towns in other provinces
            "bounded": 1,
        }
    )
    time.sleep(1.1)  # Nominatim usage policy: max 1 req/s
    res = get(NOMINATIM.format(q))
    if res:
        return float(res[0]["lat"]), float(res[0]["lon"]), "osm"
    return None


def geocode(addr, localidad):
    for f in (usig, nominatim):
        try:
            if r := f(addr, localidad):
                return r
        except OSError as e:
            print(f"  {f.__name__} error: {e}", file=sys.stderr)
    return None


def main():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    with open("cartilla.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows):
        addr = street(row["direccion"])
        key = f"{addr}|{row['localidad']}"
        if key not in cache:
            cache[key] = geocode(addr, row["localidad"])
            print(f"[{i + 1}/{len(rows)}] {key} -> {cache[key]}", file=sys.stderr)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0) + "\n")
        lat, lon, src = cache[key] or (None, None, None)
        row.update(lat=lat, lon=lon, geo=src)

    data = json.dumps(rows, ensure_ascii=False)
    Path("cartilla.js").write_text(f"const CARTILLA = {data};\n", encoding="utf-8")
    missing = [r for r in rows if r["lat"] is None]
    print(
        f"Wrote {len(rows)} rows, {len(missing)} without coordinates", file=sys.stderr
    )
    for r in missing:
        print(f"  missing: {r['direccion']}, {r['localidad']}", file=sys.stderr)


if __name__ == "__main__":
    main()
