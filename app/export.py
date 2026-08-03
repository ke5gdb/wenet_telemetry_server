"""Flight data export.

"The data should be persistent and exportable" was a requirement, so these are
deliberately plain: formats that open in pandas, a spreadsheet, or the tooling
already used on past flights, plus a lossless archive for anything a decoder
improvement might want to revisit.

Every export is a **streaming generator**. A flight can run to hundreds of
megabytes with the raw CBOR archive included, and materialising that in memory
to answer one download would take the server down mid-flight. Starlette runs a
sync generator in a threadpool, so these open their own connection and iterate a
server-side cursor rather than fetching everything up front.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import Iterator

from .db import connect

#: Flush the CSV buffer roughly every this many rows.
CHUNK_ROWS = 500


class _Buffer:
    """A file-like sink that hands accumulated text back in chunks."""

    def __init__(self):
        self._parts: list[str] = []

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)

    def drain(self) -> str:
        out = "".join(self._parts)
        self._parts.clear()
        return out


def _flight_or_die(conn: sqlite3.Connection, flight_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
    if row is None:
        raise LookupError(f"no flight {flight_id}")
    return row


def source_columns(conn: sqlite3.Connection, source_id: int) -> list[str]:
    """Field names for a source, numeric first, then text -- both alphabetical."""
    rows = conn.execute(
        "SELECT name, kind FROM fields WHERE source_id=? ORDER BY kind DESC, name",
        (source_id,),
    ).fetchall()
    return [r["name"] for r in rows]


# -- CSV ------------------------------------------------------------------


def csv_wide(db_path, flight_id: int, source_key: str) -> Iterator[str]:
    """One source, one column per field -- the shape you load into pandas.

    Mirrors the payload's own SD-card CSV, with the resolved altitude added,
    which is the column that makes the rest of the row mean something.
    """
    conn = connect(db_path)
    try:
        _flight_or_die(conn, flight_id)
        source = conn.execute(
            "SELECT * FROM sources WHERE flight_id=? AND source_key=?",
            (flight_id, source_key),
        ).fetchone()
        if source is None:
            raise LookupError(f"no source {source_key!r} in flight {flight_id}")

        columns = source_columns(conn, source["id"])
        buf = _Buffer()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["time", "count", "altitude_m", "truncated", *columns])

        cursor = conn.execute(
            "SELECT payload_time, count, altitude, truncated, data FROM packets "
            "WHERE source_id=? ORDER BY time_epoch, count",
            (source["id"],),
        )
        for i, row in enumerate(cursor, 1):
            data = json.loads(row["data"])
            writer.writerow([
                row["payload_time"], row["count"],
                "" if row["altitude"] is None else round(row["altitude"], 1),
                1 if row["truncated"] else 0,
                *[data.get(name, "") for name in columns],
            ])
            if i % CHUNK_ROWS == 0:
                yield buf.drain()
        yield buf.drain()
    finally:
        conn.close()


def csv_long(db_path, flight_id: int) -> Iterator[str]:
    """Every reading from every source in one file.

    Long rather than wide because sources do not share a field set or a rate --
    a wide table across all of them would be mostly empty cells.
    """
    conn = connect(db_path)
    try:
        _flight_or_die(conn, flight_id)
        buf = _Buffer()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["time", "source", "field", "value", "unit", "altitude_m", "count"])

        cursor = conn.execute(
            "SELECT r.ts, s.source_key, f.name, r.value, f.unit, p.altitude, p.count "
            "FROM readings r "
            "JOIN fields f ON f.id = r.field_id "
            "JOIN sources s ON s.id = f.source_id "
            "JOIN packets p ON p.id = r.packet_id "
            "WHERE s.flight_id=? ORDER BY r.ts_epoch, s.source_key, f.name",
            (flight_id,),
        )
        for i, row in enumerate(cursor, 1):
            writer.writerow([
                row["ts"], row["source_key"], row["name"], row["value"], row["unit"],
                "" if row["altitude"] is None else round(row["altitude"], 1),
                row["count"],
            ])
            if i % CHUNK_ROWS == 0:
                yield buf.drain()
        yield buf.drain()
    finally:
        conn.close()


def csv_gps(db_path, flight_id: int) -> Iterator[str]:
    conn = connect(db_path)
    try:
        _flight_or_die(conn, flight_id)
        buf = _Buffer()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["timestamp", "latitude", "longitude", "altitude_m",
                         "ascent_rate", "ground_speed", "heading", "num_sv", "gps_fix"])
        cursor = conn.execute(
            "SELECT * FROM gps WHERE flight_id=? ORDER BY time_epoch", (flight_id,)
        )
        for i, row in enumerate(cursor, 1):
            writer.writerow([row["timestamp"], row["latitude"], row["longitude"],
                             row["altitude"], row["ascent_rate"], row["ground_speed"],
                             row["heading"], row["num_sv"], row["gps_fix"]])
            if i % CHUNK_ROWS == 0:
                yield buf.drain()
        yield buf.drain()
    finally:
        conn.close()


# -- JSON Lines -----------------------------------------------------------


def jsonl(db_path, flight_id: int) -> Iterator[str]:
    """One decoded packet per line.

    Deliberately the same shape as the payload-side analysis logs from previous
    flights, so existing notebooks read an export without changes -- with
    `altitude` and `reporters` added, which those logs could not carry.
    """
    conn = connect(db_path)
    try:
        _flight_or_die(conn, flight_id)
        cursor = conn.execute(
            "SELECT p.id, p.payload_time, p.count, p.data, p.altitude, p.truncated, "
            "       s.source_key "
            "FROM packets p JOIN sources s ON s.id = p.source_id "
            "WHERE s.flight_id=? ORDER BY p.time_epoch, s.source_key",
            (flight_id,),
        )
        reporters = conn.cursor()
        out = []
        for i, row in enumerate(cursor, 1):
            record = json.loads(row["data"])
            record["id"] = row["source_key"]
            record["time"] = row["payload_time"]
            record["count"] = row["count"]
            if row["altitude"] is not None:
                record["altitude"] = round(row["altitude"], 1)
            if row["truncated"]:
                record["truncated"] = True
            record["reporters"] = [
                r[0] for r in reporters.execute(
                    "SELECT reporter_id FROM receptions WHERE packet_id=? ORDER BY reporter_id",
                    (row["id"],),
                )
            ]
            out.append(json.dumps(record, default=str))
            if i % CHUNK_ROWS == 0:
                yield "\n".join(out) + "\n"
                out = []
        if out:
            yield "\n".join(out) + "\n"
    finally:
        conn.close()


# -- raw archive ----------------------------------------------------------


def raw_cbor(db_path, flight_id: int) -> Iterator[bytes]:
    """Length-prefixed raw packets, exactly as they came off the air.

    Format: repeating [4-byte big-endian length][that many bytes of CBOR].
    A framing is needed at all because CBOR is self-delimiting only if you trust
    the packet to be whole, and these may be truncated.

    This is the archive that survives a decoder bug: re-run a fixed decoder over
    it later and recover fields the original ingest could not read.
    """
    conn = connect(db_path)
    try:
        _flight_or_die(conn, flight_id)
        cursor = conn.execute(
            "SELECT p.raw_cbor FROM packets p JOIN sources s ON s.id = p.source_id "
            "WHERE s.flight_id=? AND p.raw_cbor IS NOT NULL ORDER BY p.time_epoch",
            (flight_id,),
        )
        chunk = []
        size = 0
        for row in cursor:
            blob = bytes(row["raw_cbor"])
            chunk.append(struct.pack(">I", len(blob)))
            chunk.append(blob)
            size += len(blob) + 4
            if size >= 256 * 1024:
                yield b"".join(chunk)
                chunk, size = [], 0
        if chunk:
            yield b"".join(chunk)
    finally:
        conn.close()


# -- whole-database snapshot ----------------------------------------------


def database_snapshot(db_path) -> tuple[Path, Iterator[bytes]]:
    """A consistent copy of the whole database, taken without stopping ingest.

    `VACUUM INTO` snapshots inside a read transaction, so the copy is coherent
    even while packets are arriving -- unlike copying the file, which under WAL
    can catch a torn state.
    """
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    target = Path(handle.name)
    target.unlink()  # VACUUM INTO refuses to overwrite an existing file

    conn = connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()

    def stream() -> Iterator[bytes]:
        try:
            with target.open("rb") as fh:
                while True:
                    block = fh.read(1024 * 256)
                    if not block:
                        break
                    yield block
        finally:
            target.unlink(missing_ok=True)

    return target, stream()


def flight_slug(conn: sqlite3.Connection, flight_id: int) -> str:
    """A filename-safe stem from the flight name, for the download."""
    row = conn.execute("SELECT name, started_at FROM flights WHERE id=?", (flight_id,)).fetchone()
    base = (row["name"] if row else f"flight-{flight_id}") or f"flight-{flight_id}"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in base).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.lower() or f"flight-{flight_id}"
