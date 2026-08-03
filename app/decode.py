"""CBOR decoding, tolerant of the truncation the BLE link can inflict.

A BLE notification carries at most ``ATT MTU - 3`` bytes. A full sensor packet
reaches 254, so a host that never negotiates a larger MTU silently cuts the tail
off. The transmit path also right-pads every packet to 254 bytes with NULs
before handing it to the modem, which CBOR decoders ignore as trailing data.

The configurator already takes the view that a partial packet should be decoded
as far as it goes and flagged, rather than discarded. This does the same, so a
marginal link degrades into fewer fields instead of no data.
"""

from __future__ import annotations

import io

import cbor2

_MAJOR_MAP = 5


def decode_packet(raw: bytes) -> tuple[dict, bool]:
    """Decode a CBOR map. Returns ``(data, truncated)``.

    Never raises for malformed input -- an undecodable buffer comes back as
    ``({}, True)`` and the caller files it as a reject.
    """
    if not raw:
        return {}, True

    try:
        value = cbor2.loads(raw)
        if isinstance(value, dict):
            return value, False
        # A non-map at the top level is not something this system produces.
        return {}, True
    except Exception:
        pass

    return _decode_partial(raw)


def _decode_partial(raw: bytes) -> tuple[dict, bool]:
    """Walk a definite-length CBOR map, keeping whatever pairs are intact."""
    fp = io.BytesIO(raw)

    header = fp.read(1)
    if not header:
        return {}, True

    initial = header[0]
    if initial >> 5 != _MAJOR_MAP:
        return {}, True

    info = initial & 0x1F
    try:
        if info < 24:
            pairs = info
        elif info == 24:
            pairs = fp.read(1)[0]
        elif info == 25:
            pairs = int.from_bytes(_exact(fp, 2), "big")
        elif info == 26:
            pairs = int.from_bytes(_exact(fp, 4), "big")
        else:
            # 27 (64-bit length) or 31 (indefinite). The firmware emits neither,
            # so treat it as corruption rather than trying to be clever.
            return {}, True
    except (IndexError, ValueError):
        return {}, True

    decoder = cbor2.CBORDecoder(fp)
    out: dict = {}
    for _ in range(pairs):
        try:
            key = decoder.decode()
            value = decoder.decode()
        except Exception:
            return out, True
        if isinstance(key, (str, int)):
            out[key] = value

    # Every pair the header promised was present, so the map itself was whole;
    # cbor2.loads must have objected to something after it.
    return out, len(out) < pairs


def _exact(fp: io.BytesIO, n: int) -> bytes:
    b = fp.read(n)
    if len(b) != n:
        raise ValueError("short read")
    return b
