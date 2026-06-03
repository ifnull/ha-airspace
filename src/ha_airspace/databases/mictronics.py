"""Mictronics (tar1090-db) CSV parser.

Source: ``https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz``
Format: gzip'd, semicolon-delimited, **no header**, ~620k rows. Columns
(verified against the live file 2026-06-02):

    hex ; registration ; type ; dbFlags ; long_type ; ... (8 fields total)

* ``hex`` — ICAO 24-bit, uppercase in the file; we lowercase to match the
  observation key.
* ``registration`` — tail number (may be empty).
* ``type`` — ICAO type designator, e.g. ``B732`` (may be empty).
* ``dbFlags`` — tar1090-db bitfield, stored as a string of ``0``/``1`` chars
  written **LSB-first** (bit 0 is the leftmost character). The bit positions
  (verified against the live file: the US-military AE-block is uniformly
  ``"10"``, and the EA-18G Growler ``AE49E0`` is ``"11000"`` = mil+interesting):

      position 0 -> military   (``mil``)
      position 1 -> interesting (``interesting``)
      position 2 -> PIA         (``pia``)
      position 3 -> LADD        (``ladd``)

  So ``"10"`` = military only, ``"0001"`` = LADD, ``"0010"`` = PIA. A char
  that is not ``0``/``1`` is treated as unset (defensive).
* trailing fields — unused here.

Output: ``dict[hex_lower, dict]`` with the canonical keys the enricher and
flag matchers expect: ``reg``, ``type``, and the boolean flags
``mil``/``interesting``/``pia``/``ladd``. We emit a *sparse* dict — only keys
with a real value (non-empty string, true flag) — so the merge with ADSBex is
a plain ``dict.update`` and empty strings never shadow a populated field from
the other source.
"""

from __future__ import annotations

import gzip
import io

# dbFlags bit position -> canonical key (LSB-first: index into the string).
_DBFLAG_KEYS: tuple[str, ...] = ("mil", "interesting", "pia", "ladd")


def parse_mictronics(raw_gzip: bytes) -> dict[str, dict[str, object]]:
    """Parse the gzip'd Mictronics CSV into ``{hex_lower: {fields}}``.

    Permissive: rows with too few columns or an unparseable hex are
    skipped, not fatal — a few malformed rows in a 620k-row community DB
    should never abort the whole load.
    """
    result: dict[str, dict[str, object]] = {}
    with gzip.open(io.BytesIO(raw_gzip), mode="rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            row = line.rstrip("\n").split(";")
            if len(row) < 4:
                continue
            hex_code = row[0].strip().lower()
            if not hex_code:
                continue
            entry: dict[str, object] = {}
            reg = row[1].strip()
            if reg:
                entry["reg"] = reg
            type_code = row[2].strip()
            if type_code:
                entry["type"] = type_code
            entry.update(_parse_dbflags(row[3]))
            if entry:
                result[hex_code] = entry
    return result


def _parse_dbflags(raw: str) -> dict[str, bool]:
    """Decode the LSB-first binary dbFlags string into the set flags. Each
    character position maps to one flag (see module docstring); only set
    (``'1'``) flags are returned, so the result is sparse."""
    value = raw.strip()
    flags: dict[str, bool] = {}
    for index, key in enumerate(_DBFLAG_KEYS):
        if index < len(value) and value[index] == "1":
            flags[key] = True
    return flags


__all__ = ["parse_mictronics"]
