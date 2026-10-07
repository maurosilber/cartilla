"""Geocode a cartilla CSV into the JSON file loaded by index.html.

Tries the USIG normalizer (GCBA, covers CABA and AMBA) first, picking the
candidate whose locality matches; falls back to OpenStreetMap Nominatim.
Results are cached in a JSON file so reruns only query new addresses.
Addresses without a street number (0, S/N, corners) are left without
coordinates: the middle of the street could be kilometers off.

Usage: python3 geocode_cartilla.py cartilla.csv geocode_cache.json cartilla.json
"""

import csv
import json
import math
import re
import statistics
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

USIG = "https://servicios.usig.buenosaires.gob.ar/normalizar/?{}"
NOMINATIM = "https://nominatim.openstreetmap.org/search?{}"
UA = "up-cartilla-map/1.0"
CABA = {"capital federal", "ciudad autonoma de buenos aires"}
MAX_KM = 10  # from the median of its locality, else it's a wrong match
WORKERS = 4  # parallel USIG requests; Nominatim goes one at a time
AMBA = "-59.4,-34.0,-57.7,-35.3"  # lon_min,lat_max,lon_max,lat_min
# Abbreviations (and typos) in the source that the geocoders don't know.
ABBREV = {
    "avda": "av",
    "cnel": "coronel",
    "dr": "doctor",
    "gral": "general",
    "ing": "ingeniero",
    "maschwittz": "maschwitz",  # typo
    "mtro": "maestro",
    "pte": "presidente",
    "sta": "santa",
    "tte": "teniente",
}


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def expand(s):
    """'PTE.PERON 1896' -> 'presidente peron 1896'."""
    words = re.findall(r"[a-z0-9]+", norm(s))
    return " ".join(ABBREV.get(w, w) for w in words)


def street(direccion):
    """'Juncal 2951 - 1 A' -> 'Juncal 2951' (drop floor/unit details).

    Also drops 'N°'/'NRO' and a number the source repeats: 'SALTA 4999 4999'.
    """
    s = re.split(r"\s+-\s+|,", direccion)[0]
    s = re.sub(r"\b(N[°º]|NRO\.?)\s*(?=\d)", " ", s)
    m = re.match(r"(.*\d)", s)
    s = re.sub(r"\s+", " ", m.group(1) if m else s).strip()
    return re.sub(r"\b(\d+)(?: \1\b)+$", r"\1", s)


def has_number(addr):
    """'Junin 917' -> True; 'Belgrano 0', 'Av Sarmiento', '9' -> False."""
    m = re.search(r"\D\s*(\d+)$", addr)
    return bool(m) and int(m.group(1)) > 0


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def usig(addr, localidad):
    caba = norm(localidad) in CABA
    loc = expand(localidad)
    # Naming the locality disambiguates streets repeated across partidos.
    queries = [expand(addr)] if caba else [f"{expand(addr)}, {loc}", expand(addr)]
    for query in queries:
        q = urllib.parse.urlencode(
            {"direccion": query, "geocodificar": "TRUE", "srid": 4326, "maxOptions": 20}
        )
        for c in get(USIG.format(q)).get("direccionesNormalizadas", []):
            if not c.get("coordenadas") or c["tipo"] != "calle_altura":
                continue
            if caba:
                ok = c["cod_partido"] == "caba"
            else:
                ok = loc in (expand(c["nombre_localidad"]), expand(c["nombre_partido"]))
            if ok:
                xy = c["coordenadas"]
                return float(xy["y"]), float(xy["x"]), "usig"
    return None


def nominatim(addr, localidad):
    loc = (
        "Ciudad Autónoma de Buenos Aires"
        if norm(localidad) in CABA
        else expand(localidad)
    )
    q = urllib.parse.urlencode(
        {
            "q": f"{expand(addr)}, {loc}, Argentina",
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


def outliers(cache):
    """Keys geocoded too far from the rest of their locality (outside CABA)."""
    by_loc = {}
    for k, v in cache.items():
        loc = k.split("|")[1]
        if v and norm(loc) not in CABA:
            by_loc.setdefault(loc, []).append((k, v))
    out = set()
    for points in by_loc.values():
        if len(points) < 3:
            continue
        lat = statistics.median(v[0] for _, v in points)
        lon = statistics.median(v[1] for _, v in points)
        for k, v in points:
            dx = (v[1] - lon) * 111 * math.cos(math.radians(lat))
            if math.hypot((v[0] - lat) * 111, dx) > MAX_KM:
                out.add(k)
    return out


def try_(f, key):
    addr, localidad = key.split("|")
    for attempt in range(3):
        try:
            return f(addr, localidad)
        except OSError as e:
            print(f"  {f.__name__} error on {key}: {e}", file=sys.stderr)
            time.sleep(2)
    return None


def main(csv_path, cache_path, out):
    cache_path, out = Path(cache_path), Path(out)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    keys = [f"{street(r['direccion'])}|{r['localidad']}" for r in rows]
    new = [k for k in dict.fromkeys(keys) if k not in cache]
    numbered = [k for k in new if has_number(k.split("|")[0])]
    cache.update(dict.fromkeys(new))

    def save():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=0) + "\n")

    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (k, r) in enumerate(
            zip(numbered, ex.map(lambda k: try_(usig, k), numbered)), 1
        ):
            cache[k] = r
            print(f"[usig {i}/{len(numbered)}] {k} -> {r}", file=sys.stderr)
    save()
    misses = [k for k in numbered if cache[k] is None]
    for i, k in enumerate(misses, 1):
        cache[k] = try_(nominatim, k)
        print(f"[osm {i}/{len(misses)}] {k} -> {cache[k]}", file=sys.stderr)
        save()

    # Drop addresses no longer in the cartilla.
    cache = {k: cache[k] for k in sorted(set(keys))}
    save()
    bad = outliers(cache)
    for k in sorted(bad):
        print(f"  outlier: {k} -> {cache[k]}", file=sys.stderr)
    for row, key in zip(rows, keys):
        ok = key not in bad and has_number(key.split("|")[0])
        lat, lon, src = (ok and cache[key]) or (None, None, None)
        row.update(lat=lat, lon=lon, geo=src)

    data = json.dumps(rows, ensure_ascii=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(data + "\n", encoding="utf-8")
    missing = [r for r in rows if r["lat"] is None]
    print(
        f"Wrote {len(rows)} rows, {len(missing)} without coordinates", file=sys.stderr
    )
    for r in missing:
        print(f"  missing: {r['direccion']}, {r['localidad']}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
