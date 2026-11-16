"""Stable, process-independent digests.

Nothing here depends on Python's hash randomisation, dict insertion order or
filesystem ordering.  Group keys are bound to their column order so a key read
under one grouping can never be mistaken for the same values under another.
"""

from __future__ import annotations

import hashlib
import json
import struct
from fractions import Fraction


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def field_digest(cols) -> str:
    return "fld:" + sha256_hex(stable_json(sorted(cols)))[:32]


def _enc(v) -> bytes:
    if v is None:
        return b"N"
    if isinstance(v, bool):
        raise TypeError("bool is not a fact value")
    if isinstance(v, int):
        return b"I" + struct.pack("<q", v)
    if isinstance(v, str):
        b = v.encode("utf-8")
        return b"S" + struct.pack("<I", len(b)) + b
    raise TypeError("unsupported fact value %r" % (type(v),))


def key_digest(group_cols, values) -> str:
    h = hashlib.sha256()
    h.update(field_digest(group_cols).encode("ascii"))
    h.update(b"|")
    h.update(struct.pack("<I", len(group_cols)))
    for v in values:
        h.update(_enc(v))
    return "key:" + h.hexdigest()[:32]


def row_digest(row) -> str:
    return "st:" + sha256_hex(stable_json(dict(row)))[:32]


def reduce_fraction(num: int, den: int) -> Fraction:
    if den == 0:
        raise ZeroDivisionError("zero denominator")
    return Fraction(num, den)


def fraction_json(fr: Fraction) -> dict:
    return {"num": fr.numerator, "den": fr.denominator}
