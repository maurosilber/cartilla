"""Turn a correction issue, sent from index.html, into a file for geocode_cartilla.py.

The issue's body ends with a ```json block, written by report() in index.html:

    {"cartilla": "dosuba",
     "provider": {"nombre": ..., "direccion": ..., "localidad": ...},
     "fields": {"telefonos": ...},  # optional
     "location": [lat, lon]}        # optional

Anyone can open an issue, so it is checked against the cartilla (with the earlier
corrections applied, as the page showed it) and written to
data/<cartilla>/corrections/<issue>.json for review in a pull request.
An invalid correction exits with its reason in stderr, for the workflow to reply with.
A correction with nothing to apply (only a comment) writes nothing.

Usage: python3 correction.py $GITHUB_EVENT_PATH
"""

import csv
import json
import math
import re
import sys
from pathlib import Path

from geocode_cartilla import PROVIDER, apply_corrections, load_corrections, provider

DATA = Path("data")
FIELDS = {"direccion", "localidad", "telefonos", "horario", "especialidades"}
MAX_LEN = 500
ARGENTINA = ((-55.1, -21.7), (-73.6, -53.6))  # (lat_min, lat_max), (lon_min, lon_max)
BLOCK = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL)


class Invalid(Exception):
    pass


def parse(body):
    blocks = BLOCK.findall(body or "")
    if not blocks:
        raise Invalid("Falta el bloque ```json con la corrección.")
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        raise Invalid(f"El JSON de la corrección no es válido: {e}") from None


def check(c, number):
    """The cartilla and the correction to write (or None), from the issue's JSON."""
    if not isinstance(c, dict):
        raise Invalid("La corrección debe ser un objeto JSON.")
    cartillas = {p.parent.name for p in DATA.glob("*/cartilla.csv")}
    os_ = c.get("cartilla")
    if os_ not in cartillas:
        raise Invalid(f"`cartilla` debe ser una de: {', '.join(sorted(cartillas))}.")
    p = c.get("provider")
    if not isinstance(p, dict) or not all(isinstance(p.get(k), str) for k in PROVIDER):
        raise Invalid(f"`provider` debe tener {', '.join(PROVIDER)}.")

    folder = DATA / os_
    with open(folder / "cartilla.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    # All but this issue's, in case it is edited after being applied.
    earlier = load_corrections(folder / "corrections")
    apply_corrections(rows, [e for e in earlier if e["issue"] != number])
    row = next((r for r in rows if provider(r) == provider(p)), None)
    if row is None:
        raise Invalid(
            "El prestador no está en la cartilla (puede haber cambiado desde que se envió)."
        )

    fields = c.get("fields") or {}
    if not isinstance(fields, dict) or not fields.keys() <= FIELDS & row.keys():
        raise Invalid(
            f"`fields` solo puede tener {', '.join(sorted(FIELDS & row.keys()))}."
        )
    for k, v in fields.items():
        if not isinstance(v, str) or len(v) > MAX_LEN:
            raise Invalid(f"`{k}` debe ser un texto de hasta {MAX_LEN} caracteres.")
    fields = {
        k: " ".join(v.split())
        for k, v in fields.items()
        if " ".join(v.split()) != row[k]
    }

    loc = c.get("location")
    if loc is not None:
        if not (
            isinstance(loc, list)
            and len(loc) == 2
            and all(type(x) in (int, float) and math.isfinite(x) for x in loc)
            and all(lo <= x <= hi for x, (lo, hi) in zip(loc, ARGENTINA))
        ):
            raise Invalid(
                "`location` debe ser [latitud, longitud] dentro de Argentina."
            )
        loc = [round(x, 6) for x in loc]

    if not fields and loc is None:
        return os_, None
    out = {"provider": {k: p[k] for k in PROVIDER}}
    if fields:
        out["fields"] = fields
    if loc is not None:
        out["location"] = loc
    return os_, out


def main(event_path):
    issue = json.loads(Path(event_path).read_text(encoding="utf-8"))["issue"]
    try:
        os_, correction = check(parse(issue["body"]), issue["number"])
    except Invalid as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    if correction is None:
        print("Nothing to apply.", file=sys.stderr)
        return
    path = DATA / os_ / "corrections" / f"{issue['number']}.json"
    path.parent.mkdir(exist_ok=True)
    data = {"issue": issue["number"], **correction}
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
