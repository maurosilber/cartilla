"""Geocode a cartilla CSV into the JSON file loaded by index.html.

Tries the USIG normalizer (GCBA, covers CABA and AMBA) first, picking the
candidate whose locality matches; falls back to OpenStreetMap Nominatim.
Results are cached in a JSON file so reruns only query new addresses.
Addresses without a street number (0, S/N, corners) are left without
coordinates: the middle of the street could be kilometers off.

The corrections sent from the page (data/<os>/corrections/<issue>.json, written by
correction.py) are applied first: their fields replace the cartilla's, and their
locations replace the geocoders'.

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
PROVIDER = ("nombre", "direccion", "localidad")  # what identifies a provider
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
# The many spellings of WhatsApp next to phone numbers (whattsap, WHATSAAP, WA, WPP, ...),
# written as "WhatsApp" for index.html to show as an icon.
# Not \b, as some are glued to the number ("15-5922-9731wattsapp").
WHATSAPP = re.compile(
    r"(?<![a-z])(?:wh?a+t+s+a+p+|wa|wat|wath|wh|wha|whap|what|wht|whts|wp|wpp|ws|wsp)(?![a-z])",
    re.IGNORECASE,
)
# Phone numbers, with the "+54 011" Unión Personal puts in front of all of them,
# even those with another area code ("+54 011 0220-4850337") or none ("+54 011 -").
PREFIX = r"\(?\+54\)?[\s-]*(?:0?11\b)?[\s-]*"
PHONE = re.compile(rf"{PREFIX}(?:\d[\d\s()-]*\d|\d)?|\d(?:[\d\s()-]*\d)?")
# Area codes of 3 digits (after the 0); 11 has 2 and the rest 4.
AREA3 = set(
    re.findall(
        r"\d+",
        "220 221 223 230 236 237 249 260 261 263 264 266 280 291 294 297 298 299 336 341"
        " 342 343 345 351 353 358 362 364 370 376 379 380 381 383 385 387 388",
    )
)


def format_number(s):
    """'011-43891481' -> '4389-1481'; '0237 4883741' -> '0237 488-3741'.

    Local numbers of Buenos Aires (area code 11) are written without it, mobiles
    as '15-5700-0787' or '11 5700-0787'. Returns None when not a known shape.
    """
    s = re.sub(rf"^{PREFIX}", "", s)
    digits = re.sub(r"\D", "", s)
    if re.fullmatch(r"0?8[01]0\d{7}", digits):
        d = digits.removeprefix("0")
        return f"0{d[:3]}-{d[3:6]}-{d[6:]}"
    if len(digits) == 10 and digits.startswith("11"):
        return f"11 {digits[2:6]}-{digits[6:]}"
    # With the area code, but not its 0: '221 155316666', '3484624728'.
    national = len(digits) == 12 or len(digits) == 10 and digits[:1] != "1"
    if national and digits[:1] != "0":
        digits = "0" + digits
    if digits.startswith("0"):
        # Trust a separator after the area code, else the list of area codes.
        if digits[1:3] == "11":
            n = 2
        elif m := re.match(r"0?(\d{3,4})\D", s):
            n = len(m.group(1))
        else:
            n = 3 if digits[1:4] in AREA3 else 4
        area, local = digits[1 : n + 1], digits[n + 1 :]
        if area == "11" and len(local) == 10 and local.startswith("11"):
            return format_number(local)  # '011 1156926080'
    else:
        area, local = "11", digits
    mobile = local.startswith("15") and len(area) + len(local) == 12
    local = local[2:] if mobile else local
    if len(area) + len(local) != 10:
        return None
    local = f"{'15-' if mobile else ''}{local[:-4]}-{local[-4:]}"
    return local if area == "11" else f"0{area} {local}"


def format_phones(s):
    """Formats the phone numbers in s, leaving the rest as is.

    Some are two numbers separated by a space ('15-6769-8923 15-5710-9264').
    """

    def sub(m):
        number = m.group()
        # Also the placeholders: '0', '000', '011-0'.
        if re.fullmatch(r"0*(?:11)?0*", re.sub(rf"^{PREFIX}|\D", "", number)):
            return ""
        if (f := format_number(number)) is not None:
            return f
        parts = re.split(r"\s+(?=\d)", number)
        formatted = [format_number(p) for p in parts]
        if len(parts) > 1 and None not in formatted:
            return " ".join(formatted)
        # Without the prefix, unless it holds the only area code ('+54 221 1553').
        if (p := re.match(PREFIX, number)) and (
            "11" in p.group() or number[p.end() :].startswith("0")
        ):
            number = number[p.end() :]
        return re.sub(r"\s*-[\s-]*", "-", number).strip("-")

    s = PHONE.sub(sub, s)
    # Dashes left next to the removed prefixes and the separators.
    s = re.sub(r"(?<![\w)])-+|-+(?![\w(])", "", s)
    s = re.sub(r"\s+,", ",", s)
    return re.sub(r"\s+", " ", s).strip(" ,|/")


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


def load_corrections(folder):
    """The corrections in a folder, oldest issue first."""
    files = sorted(Path(folder).glob("*.json"), key=lambda p: int(p.stem))
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def provider(row):
    return tuple(row[k] for k in PROVIDER)


def apply_corrections(rows, corrections):
    """Applies the corrections' fields to the rows, in place.

    Returns the corrections' locations: by address key when it has a street number
    (fixing every provider there), else by row index. Corrections whose provider
    is no longer in the cartilla are returned apart, as stale.
    """
    by_provider = {}
    for i, r in enumerate(rows):
        by_provider.setdefault(provider(r), []).append(i)
    by_key, by_row, stale = {}, {}, []
    for c in corrections:
        # A provider is named as the page showed it, i.e. after the earlier corrections.
        idx = by_provider.pop(provider(c["provider"]), None)
        if not idx:
            stale.append(c)
            continue
        fields = c.get("fields", {})
        for i in idx:
            rows[i].update(fields)
            # DOSUBA's link to Google Maps points to the old address; index.html makes one.
            if {"direccion", "localidad"} & fields.keys() and "maps_url" in rows[i]:
                rows[i]["maps_url"] = ""
        r = rows[idx[0]]
        by_provider.setdefault(provider(r), []).extend(idx)
        if loc := c.get("location"):
            addr = street(r["direccion"])
            if has_number(addr):
                by_key[f"{addr}|{r['localidad']}"] = loc
            else:
                by_row.update(dict.fromkeys(idx, loc))
    return by_key, by_row, stale


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
    corrections = load_corrections(Path(csv_path).parent / "corrections")
    by_key, by_row, stale = apply_corrections(rows, corrections)
    for c in stale:
        p = c["provider"]
        print(
            f"  stale correction #{c['issue']}: {p['nombre']}, {p['direccion']}, "
            f"{p['localidad']} is no longer in the cartilla",
            file=sys.stderr,
        )
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
    for i, (row, key) in enumerate(zip(rows, keys)):
        ok = key not in bad and has_number(key.split("|")[0])
        lat, lon, *_ = (
            by_row.get(i) or by_key.get(key) or (ok and cache[key]) or (None, None)
        )
        row.update(lat=lat, lon=lon)

    for row in rows:
        for k in ("telefonos", "horario"):
            if row.get(k):
                row[k] = re.sub(
                    r"(?<=\d)WhatsApp", " WhatsApp", WHATSAPP.sub("WhatsApp", row[k])
                )
        if row.get("telefonos"):
            row["telefonos"] = format_phones(row["telefonos"])

    # The source lists a provider once per plan; merge rows that differ only in plan.
    merged = {}
    for row in rows:
        plan = row.pop("plan")
        r = merged.setdefault(tuple(row.values()), {**row, "plans": []})
        r["plans"].append(plan)
    # And once per specialty; merge rows that differ only in them. Not if the plans
    # differ too, or a plan would get specialties it doesn't cover.
    by_esps = {}
    for row in merged.values():
        esps = row.pop("especialidades").split(",")
        key = json.dumps(row, ensure_ascii=False)
        r = by_esps.setdefault(key, {**row, "especialidades": []})
        r["especialidades"] += [e for e in esps if e and e not in r["especialidades"]]
    rows = list(by_esps.values())
    for r in rows:
        r["especialidades"] = ",".join(sorted(r["especialidades"]))

    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
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
