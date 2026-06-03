"""ADSBexchange basic-ac-db parser.

Source: ``https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz``
Format: gzip'd, **newline-delimited JSON** (one object per line, NOT a JSON
array), ~615k rows. Each object (verified against the live file 2026-06-02):

    {"icao":"ae292b","reg":"164386","icaotype":"E6","year":null,
     "manufacturer":"BOEING","model":"E-6B Mercury","ownop":null,
     "faa_pia":false,"faa_ladd":false,"short_type":"L4J","mil":true}

We map to the canonical keys the enricher / flag matchers expect:

* ``icao``     -> dict key (lowercased)
* ``reg``      -> ``reg``
* ``icaotype`` -> ``type``
* ``model``    -> ``model``
* ``ownop``    -> ``ownop`` (owner/operator)
* ``mil``      -> ``mil``  (bool)
* ``faa_pia``  -> ``pia``  (bool)
* ``faa_ladd`` -> ``ladd`` (bool)

basic-ac-db has **no ``interesting`` field**; the ``adsbexchange:interesting``
flag ref therefore never matches (honest — the source does not carry it).

Like the Mictronics parser, output is *sparse*: false booleans and null/empty
strings are omitted so the merge is a plain ``dict.update`` and ADSBex's
populated fields cleanly win over Mictronics' on conflict (DESIGN §4).
"""

from __future__ import annotations

import gzip
import io
import json

# (source field, canonical key) for the string fields we keep.
_STR_FIELDS: tuple[tuple[str, str], ...] = (
    ("reg", "reg"),
    ("icaotype", "type"),
    ("model", "model"),
    ("ownop", "ownop"),
)
# (source field, canonical key) for the boolean flag fields.
_BOOL_FIELDS: tuple[tuple[str, str], ...] = (
    ("mil", "mil"),
    ("faa_pia", "pia"),
    ("faa_ladd", "ladd"),
)


def parse_adsbexchange(raw_gzip: bytes) -> dict[str, dict[str, object]]:
    """Parse the gzip'd newline-delimited JSON into ``{hex_lower: {fields}}``.

    Permissive: a line that is not valid JSON, or lacks an ``icao``, is
    skipped — one bad line should not abort a 600k-row load.
    """
    result: dict[str, dict[str, object]] = {}
    with gzip.open(io.BytesIO(raw_gzip), mode="rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            icao = record.get("icao")
            if not isinstance(icao, str) or not icao:
                continue
            entry: dict[str, object] = {}
            for src, key in _STR_FIELDS:
                val = record.get(src)
                if isinstance(val, str) and val:
                    entry[key] = val
            for src, key in _BOOL_FIELDS:
                if record.get(src) is True:
                    entry[key] = True
            if entry:
                result[icao.lower()] = entry
    return result


__all__ = ["parse_adsbexchange"]
