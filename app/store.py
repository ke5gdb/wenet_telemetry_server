"""Ingest, deduplication and persistence.

The one genuinely subtle thing in here is deduplication. Several ground stations
hear the same transmission -- that is the point of having several ground
stations -- and each reports it independently with its own callsign. Stored
naively, a flight received by three stations would plot every series three
times. So a packet is stored once, and the fact that N stations heard it is
recorded separately, where it becomes useful coverage data instead of noise.
"""

from __future__ import annotations

import bisect
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

from . import decode as cbor_decode
from .db import ConnectionPool, init_db
from .timeutil import iso, iso_now, parse_ts, to_epoch
from .units import RESERVED, field_meta

log = logging.getLogger(__name__)

#: Columns of the GPS table that arrive as named fields; anything else in a GPS
#: packet is preserved in `extra` rather than dropped.
_GPS_COLUMNS = {
    "latitude": "latitude",
    "longitude": "longitude",
    "altitude": "altitude",
    "ascent_rate": "ascent_rate",
    "ground_speed": "ground_speed",
    "heading": "heading",
    "numSV": "num_sv",
    "num_sv": "num_sv",
    "gpsFix": "gps_fix",
    "gps_fix": "gps_fix",
}


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    gps_accepted: int = 0
    gps_duplicates: int = 0
    altitudes_filled: int = 0
    flight_id: int | None = None
    #: Every flight this batch touched. A batch is a time window and not a
    #: source, so one POST can legitimately carry two transmitters -- each of
    #: which is its own flight and needs its own `last_activity` bump.
    flight_ids: set[int] = dc_field(default_factory=set)
    new_sources: list[str] = dc_field(default_factory=list)
    new_fields: list[str] = dc_field(default_factory=list)
    reasons: list[str] = dc_field(default_factory=list)
    #: Newest packet seen per source in this batch, and any GPS fixes -- the
    #: payload of the live update pushed to browsers. Not part of the HTTP
    #: response; consumed by the SSE hub.
    latest: dict = dc_field(default_factory=dict)
    gps_points: list = dc_field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "gps_accepted": self.gps_accepted,
            "gps_duplicates": self.gps_duplicates,
            "altitudes_filled": self.altitudes_filled,
            "flight_id": self.flight_id,
            "new_sources": self.new_sources,
            "new_fields": self.new_fields,
            "reasons": self.reasons[:10],
        }


def derive_names(source_key: str, data: dict) -> tuple[str, str]:
    """Best-effort split of a source key into payload and task names.

    Purely a display hint -- ``source_key`` remains the identity. Current
    firmware builds the id as ``payload_name + '_' + sensor_name``, but that is
    not guaranteed: the RAB-8 flight logs carry ``id: "generic"`` with the real
    name in a separate ``payload`` field, so an explicit ``payload`` always wins
    over splitting on an underscore that may not be there.
    """
    explicit = data.get("payload")
    if isinstance(explicit, str) and explicit.strip():
        payload = explicit.strip()
        # `source_identity` may already have qualified the key with this same
        # name. Show the sensor, not "XBnd RPT_generic" under a heading that
        # already says XBnd RPT.
        prefix = payload + "_"
        task = source_key[len(prefix):] if source_key.startswith(prefix) else source_key
        return payload, task

    head, sep, tail = source_key.rpartition("_")
    if sep and head:
        return head, tail
    return source_key, ""


def source_identity(raw_id: str, data: dict) -> str:
    """The key a source is filed under, qualified when the id cannot stand alone.

    ``source_key`` is unique per flight, and current firmware makes that true by
    construction: the id is ``payload_name + '_' + sensor_name``. The legacy
    format does not -- it sends ``id: "generic"`` with the real name in a
    separate ``payload`` field -- so two BLE payloads on one transmitter both
    sending ``generic`` would land on one source row, union their fields, and
    then lose packets to the ``(source_id, payload_time, count)`` dedup every
    time their counts and timestamps coincided. Two payloads counting from boot
    do that constantly.

    Only the ids that need it are rewritten. A key that already carries its
    payload name is returned byte-identical, so nothing that works today moves.
    """
    payload = data.get("payload")
    if not isinstance(payload, str) or not payload.strip():
        return raw_id
    payload = payload.strip()
    if raw_id == payload or raw_id.startswith(payload + "_"):
        return raw_id
    return f"{payload}_{raw_id}"


def interpolate_altitude(
    times: list[float], alts: list[float], t: float, window: float
) -> float | None:
    """Altitude at time `t` from a sorted GPS track, or None if too far away.

    Sensor packets and GPS fixes never share a timestamp -- GPS runs about 1 Hz
    on the receiver's clock, sensors 2 Hz on the payload RTC -- so an altitude
    always has to be interpolated rather than looked up.

    `window` bounds how far a fix may be reached for. At a 5 m/s ascent a
    10 s reach is a ~50 m error, which is fine against a 30 km profile; across a
    GPS dropout of minutes it would be pure invention. Beyond the window this
    returns None, so the plot shows an honest gap instead of a smooth lie.
    """
    if not times:
        return None

    i = bisect.bisect_left(times, t)
    if i < len(times) and times[i] == t:
        return alts[i]

    left = i - 1 if i > 0 else None
    right = i if i < len(times) else None

    # Bracketed on both sides and both near enough: linear interpolation.
    if left is not None and right is not None:
        gap_left = t - times[left]
        gap_right = times[right] - t
        if gap_left <= window and gap_right <= window:
            span = times[right] - times[left]
            if span <= 0:
                return alts[left]
            return alts[left] + (alts[right] - alts[left]) * (gap_left / span)

    # Otherwise hold the nearest fix, if one is within the window at all. This
    # covers the ends of the track and the edges of a dropout.
    candidates = []
    if left is not None:
        candidates.append((t - times[left], alts[left]))
    if right is not None:
        candidates.append((times[right] - t, alts[right]))
    near = [c for c in candidates if c[0] <= window]
    return min(near)[1] if near else None


class Store:
    def __init__(self, path, flight_gap_minutes: int = 30, altitude_window: float = 10.0):
        self.pool = ConnectionPool(path)
        self.flight_gap_seconds = flight_gap_minutes * 60
        self.altitude_window = altitude_window
        self._write_lock = threading.Lock()
        #: Flights closed mid-batch, to be altitude-flushed once the write lock
        #: is released -- backfill takes the lock itself.
        self._pending_flush: list[int] = []
        # (flight_id, source_key) -> source_id, and (source_id, name) -> field_id.
        # Single process, so an in-memory cache is safe and saves two queries
        # per field per packet.
        self._source_cache: dict[tuple[int, str], int] = {}
        self._field_cache: dict[tuple[int, str], int] = {}
        init_db(self.pool.get())

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self.pool.close_all()

    # -- ingest -----------------------------------------------------------

    def ingest(
        self,
        reporter_id: str,
        items: Iterable[dict],
        received_default: str | None = None,
    ) -> IngestResult:
        """Store a batch. The whole batch is one transaction."""
        reporter_id = (reporter_id or "UNKNOWN").strip() or "UNKNOWN"
        now = received_default or iso_now()
        result = IngestResult()
        conn = self.pool.get()

        with self._write_lock, conn:
            self._close_stale_flights(conn, now)

            for item in items:
                try:
                    self._ingest_one(conn, reporter_id, item, now, result)
                except Exception as exc:  # never let one bad packet kill a batch
                    log.warning("ingest error: %s", exc, exc_info=True)
                    self._reject(conn, reporter_id, now, f"error: {exc}", item, result)

            # Every flight the batch touched, not just the last one seen: with a
            # flight per transmitter, bumping only one of them would leave the
            # other looking silent and get it closed by its own gap.
            for flight_id in result.flight_ids:
                conn.execute(
                    "UPDATE flights SET last_activity=? WHERE id=? AND last_activity<?",
                    (now, flight_id, now),
                )

        # Outside the transaction above: resolving altitudes takes the write
        # lock itself. Cheap when nothing is pending, thanks to the partial index.
        while self._pending_flush:
            self.backfill_altitudes(self._pending_flush.pop(), flush=True)

        if result.flight_id is not None:
            result.altitudes_filled = self.backfill_altitudes(result.flight_id)["filled"]

        return result

    def _ingest_one(
        self,
        conn: sqlite3.Connection,
        reporter_id: str,
        item: dict,
        now: str,
        result: IngestResult,
    ) -> None:
        kind = (item.get("kind") or "sensor").lower()
        received_at = item.get("received_at")
        received_at = iso(received_at) if received_at else now

        raw_hex = item.get("raw")
        raw_bytes: bytes | None = None
        data = item.get("data")
        truncated = False

        if raw_hex:
            try:
                raw_bytes = bytes.fromhex(raw_hex)
            except ValueError:
                raw_bytes = None
            if raw_bytes is not None and kind == "sensor":
                # The raw bytes are authoritative -- re-decoding here means the
                # archive does not inherit any decoding bug the forwarder had,
                # and lets a future decoder improvement be replayed offline.
                decoded, truncated = cbor_decode.decode_packet(raw_bytes)
                if decoded:
                    data = decoded
                elif data:
                    truncated = True

        if not isinstance(data, dict) or not data:
            self._reject(conn, reporter_id, received_at, "no decodable data", item, result)
            return

        callsign = item.get("payload_callsign")
        if isinstance(callsign, str):
            callsign = callsign.strip() or None
        elif callsign is not None:
            callsign = None

        if kind == "gps":
            self._ingest_gps(conn, reporter_id, data, received_at, result, callsign)
        else:
            self._ingest_sensor(
                conn, reporter_id, data, raw_bytes, truncated, received_at, result, callsign
            )

    def _ingest_sensor(
        self,
        conn: sqlite3.Connection,
        reporter_id: str,
        data: dict,
        raw_bytes: bytes | None,
        truncated: bool,
        received_at: str,
        result: IngestResult,
        callsign: str | None = None,
    ) -> None:
        raw_id = data.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            self._reject(conn, reporter_id, received_at, "missing id", data, result)
            return
        source_key = source_identity(raw_id, data)

        raw_time = data.get("time")
        if raw_time is None:
            self._reject(conn, reporter_id, received_at, "missing time", data, result)
            return
        try:
            payload_time = iso(raw_time)
        except ValueError as exc:
            self._reject(conn, reporter_id, received_at, f"bad time: {exc}", data, result)
            return

        count = data.get("count")
        if not isinstance(count, int):
            # A truncated packet can lose `count`, which sits third in the map.
            # Keep the sample -- payload_time still identifies it -- but mark it.
            count = -1
            truncated = True

        flight_id = self._current_flight(conn, received_at, callsign)
        result.flight_id = flight_id
        result.flight_ids.add(flight_id)
        source_id = self._get_source(
            conn, flight_id, source_key, data, payload_time, result
        )

        cur = conn.execute(
            "INSERT OR IGNORE INTO packets"
            "(source_id, payload_time, count, time_epoch, data, raw_cbor, truncated,"
            " first_heard, payload_callsign) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                payload_time,
                count,
                to_epoch(payload_time),
                json.dumps(data, default=str),
                raw_bytes,
                int(truncated),
                received_at,
                callsign,
            ),
        )

        if cur.rowcount:
            packet_id = cur.lastrowid
            result.accepted += 1
            self._store_readings(conn, source_id, packet_id, payload_time, data, result)
            conn.execute(
                "UPDATE sources SET last_seen=MAX(last_seen, ?), "
                "packet_count=packet_count+1 WHERE id=?",
                (payload_time, source_id),
            )
            # Keep only the newest packet per source for the live push: a batch
            # may hold thirty samples, but the display only shows the latest.
            previous = result.latest.get(source_key)
            if previous is None or payload_time > previous["time"]:
                result.latest[source_key] = {
                    "time": payload_time,
                    "count": count,
                    "truncated": bool(truncated),
                    "data": data,
                    # Tagged because one batch can now span two transmitters,
                    # and the live push has to be split back apart by flight
                    # before it reaches a browser watching one of them.
                    "flight_id": flight_id,
                }
        else:
            packet_id = conn.execute(
                "SELECT id FROM packets WHERE source_id=? AND payload_time=? AND count=?",
                (source_id, payload_time, count),
            ).fetchone()[0]
            result.duplicates += 1

        # A second station hearing the same packet is receive diversity, not a
        # duplicate sample -- record who heard it, keep one sample.
        conn.execute(
            "INSERT OR IGNORE INTO receptions(packet_id, reporter_id, received_at) "
            "VALUES (?,?,?)",
            (packet_id, reporter_id, received_at),
        )

    def _store_readings(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        packet_id: int,
        ts: str,
        data: dict,
        result: IngestResult,
    ) -> None:
        rows = []
        ts_epoch = to_epoch(ts)
        for name, value in data.items():
            if name in RESERVED:
                continue
            if isinstance(value, bool):
                numeric: float | None = float(value)
            elif isinstance(value, (int, float)):
                numeric = float(value)
            else:
                numeric = None

            kind = "numeric" if numeric is not None else "text"
            field_id = self._get_field(conn, source_id, name, kind, ts, result)
            if numeric is not None:
                rows.append((field_id, ts, ts_epoch, numeric, packet_id))

        if rows:
            conn.executemany(
                "INSERT INTO readings(field_id, ts, ts_epoch, value, packet_id) "
                "VALUES (?,?,?,?,?)",
                rows,
            )

    def _ingest_gps(
        self,
        conn: sqlite3.Connection,
        reporter_id: str,
        data: dict,
        received_at: str,
        result: IngestResult,
        callsign: str | None = None,
    ) -> None:
        raw_time = data.get("timestamp") or data.get("time")
        time_source = "gps"
        if raw_time is None:
            # A receiver that has never locked has no usable clock, and the
            # forwarder drops the field rather than sending a 1980 timestamp
            # that every such packet would share. Reception time keeps them as a
            # series instead of collapsing them onto one instant.
            ts = received_at
            time_source = "receiver"
        else:
            try:
                ts = iso(raw_time)
            except ValueError as exc:
                self._reject(conn, reporter_id, received_at,
                             f"gps bad time: {exc}", data, result)
                return

        flight_id = self._current_flight(conn, received_at, callsign)
        result.flight_id = flight_id
        result.flight_ids.add(flight_id)

        values: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in data.items():
            if key in ("timestamp", "time"):
                continue
            column = _GPS_COLUMNS.get(key)
            if column:
                values[column] = value
            else:
                extra[key] = value
        extra.setdefault("time_source", time_source)

        cur = conn.execute(
            "INSERT OR IGNORE INTO gps"
            "(flight_id, timestamp, time_epoch, latitude, longitude, altitude, ascent_rate,"
            " ground_speed, heading, num_sv, gps_fix, extra, first_heard,"
            " payload_callsign) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                flight_id,
                ts,
                to_epoch(ts),
                values.get("latitude"),
                values.get("longitude"),
                values.get("altitude"),
                values.get("ascent_rate"),
                values.get("ground_speed"),
                values.get("heading"),
                values.get("num_sv"),
                values.get("gps_fix"),
                json.dumps(extra, default=str),
                received_at,
                callsign,
            ),
        )

        if cur.rowcount:
            gps_id = cur.lastrowid
            result.gps_accepted += 1
            result.gps_points.append({
                "t": ts,
                "lat": values.get("latitude"),
                "lon": values.get("longitude"),
                "alt": values.get("altitude"),
                "ascent_rate": values.get("ascent_rate"),
                "num_sv": values.get("num_sv"),
                "flight_id": flight_id,
            })
        else:
            gps_id = conn.execute(
                "SELECT id FROM gps WHERE flight_id=? AND timestamp=?", (flight_id, ts)
            ).fetchone()[0]
            result.gps_duplicates += 1

        conn.execute(
            "INSERT OR IGNORE INTO gps_receptions(gps_id, reporter_id, received_at) "
            "VALUES (?,?,?)",
            (gps_id, reporter_id, received_at),
        )

    # -- altitude backfill ------------------------------------------------

    def backfill_altitudes(
        self,
        flight_id: int,
        window: float | None = None,
        force: bool = False,
        flush: bool = False,
    ) -> dict:
        """Resolve `packets.altitude` from the GPS track.

        A packet is normally only *decidable* once GPS exists past its time by
        at least one window: until then a nearer fix may still be in flight, and
        resolving early would lock in a worse answer than the one about to
        arrive. The cost is that the last few seconds of a flight stay pending
        forever, since nothing will ever arrive past them -- `flush` resolves
        those against whatever track exists, and is applied automatically when a
        flight is closed.
        """
        window = self.altitude_window if window is None else window
        conn = self.pool.get()

        with self._write_lock, conn:
            if force:
                conn.execute(
                    "UPDATE packets SET altitude=NULL, altitude_resolved=0 "
                    "WHERE source_id IN (SELECT id FROM sources WHERE flight_id=?)",
                    (flight_id,),
                )

            gps_max = conn.execute(
                "SELECT MAX(time_epoch) FROM gps WHERE flight_id=? AND altitude IS NOT NULL",
                (flight_id,),
            ).fetchone()[0]
            if gps_max is None:
                return {"resolved": 0, "filled": 0, "unavailable": 0, "pending": 0}

            cutoff = float("inf") if flush else gps_max - window
            pending = conn.execute(
                "SELECT p.id, p.time_epoch FROM packets p JOIN sources s ON s.id=p.source_id "
                "WHERE s.flight_id=? AND p.altitude_resolved=0 AND p.time_epoch<=? "
                "ORDER BY p.time_epoch",
                (flight_id, cutoff),
            ).fetchall()
            if not pending:
                return {"resolved": 0, "filled": 0, "unavailable": 0, "pending": 0}

            # Only the stretch of track that could serve these packets.
            track = conn.execute(
                "SELECT time_epoch, altitude FROM gps "
                "WHERE flight_id=? AND altitude IS NOT NULL AND time_epoch BETWEEN ? AND ? "
                "ORDER BY time_epoch",
                (flight_id, pending[0]["time_epoch"] - window, pending[-1]["time_epoch"] + window),
            ).fetchall()
            times = [row["time_epoch"] for row in track]
            alts = [row["altitude"] for row in track]

            updates = [
                (interpolate_altitude(times, alts, row["time_epoch"], window), row["id"])
                for row in pending
            ]
            conn.executemany(
                "UPDATE packets SET altitude=?, altitude_resolved=1 WHERE id=?", updates
            )

            filled = sum(1 for alt, _ in updates if alt is not None)
            still_pending = conn.execute(
                "SELECT COUNT(*) FROM packets p JOIN sources s ON s.id=p.source_id "
                "WHERE s.flight_id=? AND p.altitude_resolved=0",
                (flight_id,),
            ).fetchone()[0]

        if filled != len(updates):
            log.info(
                "flight %s: %d packets had no GPS fix within %.0fs",
                flight_id, len(updates) - filled, window,
            )
        return {
            "resolved": len(updates),
            "filled": filled,
            "unavailable": len(updates) - filled,
            "pending": still_pending,
        }

    # -- lookups ----------------------------------------------------------

    def _current_flight(
        self, conn: sqlite3.Connection, now: str, callsign: str | None = None
    ) -> int:
        """The open flight for one transmitter, or a new one.

        Keyed on the callsign as well as the clock, because a flight is one
        Wenet transmitter's up-burst-down. Any number of BLE sensor payloads
        may be talking to that transmitter and any number of sensors may sit on
        each of them -- those are sources *within* the flight, not flights of
        their own. What must not share a flight is two transmitters, whose
        times overlap freely and whose GPS streams are two different balloons.

        ``IS`` rather than ``=`` so the no-callsign case matches itself: in
        SQLite ``NULL = NULL`` is NULL, which would open a fresh flight for
        every untagged packet.
        """
        row = conn.execute(
            "SELECT id, last_activity FROM flights "
            "WHERE ended_at IS NULL AND payload_callsign IS ? "
            "ORDER BY last_activity DESC LIMIT 1",
            (callsign,),
        ).fetchone()

        if row is not None:
            gap = (parse_ts(now) - parse_ts(row["last_activity"])).total_seconds()
            if gap <= self.flight_gap_seconds:
                return row["id"]
            # Silence long enough to call it: close the old flight at its last
            # real activity rather than now, so the record does not claim hours
            # of flight time that were actually silence.
            self._end_flight(conn, row["id"], row["last_activity"])

        return self._create_flight(conn, now, callsign)

    def _end_flight(self, conn: sqlite3.Connection, flight_id: int, ended_at: str) -> None:
        conn.execute("UPDATE flights SET ended_at=? WHERE id=?", (ended_at, flight_id))
        self._source_cache.clear()
        self._field_cache.clear()
        # Nothing more will arrive for that flight, so the tail of it can be
        # resolved now against whatever track it ended up with.
        self._pending_flush.append(flight_id)

    def _close_stale_flights(self, conn: sqlite3.Connection, now: str) -> None:
        """End every flight that has gone quiet, not just the one being written.

        Before callsigns there was one open flight and the next packet always
        closed it. Now several transmitters can be up at once, and a flight is
        otherwise only ever closed by traffic from *its own* transmitter -- so
        one that lands, is switched off or is lost would sit ``open`` forever,
        which the display reads as live. Once per batch, not per packet: there
        are only ever a handful of open flights, but there is no reason to scan
        them 15 times for one POST.
        """
        cutoff = parse_ts(now)
        for row in conn.execute(
            "SELECT id, last_activity FROM flights WHERE ended_at IS NULL"
        ).fetchall():
            gap = (cutoff - parse_ts(row["last_activity"])).total_seconds()
            if gap > self.flight_gap_seconds:
                self._end_flight(conn, row["id"], row["last_activity"])

    def _create_flight(
        self, conn: sqlite3.Connection, now: str, callsign: str | None = None
    ) -> int:
        day = now[:10]
        # The callsign leads the name because it is the identity; the date only
        # distinguishes one flight of that transmitter from the next. Counting
        # by name rather than by date, so two transmitters flying the same day
        # are "KE5GDB 2026-08-15" and "W5XYZ 2026-08-15" -- not "#2".
        base = f"{callsign} {day}" if callsign else f"Flight {day}"
        same = conn.execute(
            "SELECT COUNT(*) FROM flights WHERE name = ? OR name LIKE ?",
            (base, base + " #%"),
        ).fetchone()[0]
        name = base if not same else f"{base} #{same + 1}"
        cur = conn.execute(
            "INSERT INTO flights(name, started_at, last_activity, payload_callsign) "
            "VALUES (?,?,?,?)",
            (name, now, now, callsign),
        )
        log.info("opened flight %s (id=%s)", name, cur.lastrowid)
        return cur.lastrowid

    def _get_source(
        self,
        conn: sqlite3.Connection,
        flight_id: int,
        source_key: str,
        data: dict,
        payload_time: str,
        result: IngestResult,
    ) -> int:
        cached = self._source_cache.get((flight_id, source_key))
        if cached is not None:
            return cached

        row = conn.execute(
            "SELECT id FROM sources WHERE flight_id=? AND source_key=?",
            (flight_id, source_key),
        ).fetchone()

        if row is None:
            payload_name, task_name = derive_names(source_key, data)
            cur = conn.execute(
                "INSERT INTO sources"
                "(flight_id, source_key, payload_name, task_name, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?)",
                (flight_id, source_key, payload_name, task_name, payload_time, payload_time),
            )
            source_id = cur.lastrowid
            result.new_sources.append(source_key)
            log.info("discovered source %s (payload=%s task=%s)", source_key, payload_name, task_name)
        else:
            source_id = row["id"]

        self._source_cache[(flight_id, source_key)] = source_id
        return source_id

    def _get_field(
        self,
        conn: sqlite3.Connection,
        source_id: int,
        name: str,
        kind: str,
        ts: str,
        result: IngestResult,
    ) -> int:
        cached = self._field_cache.get((source_id, name))
        if cached is not None:
            return cached

        row = conn.execute(
            "SELECT id FROM fields WHERE source_id=? AND name=?", (source_id, name)
        ).fetchone()

        if row is None:
            unit, description = field_meta(name)
            cur = conn.execute(
                "INSERT INTO fields(source_id, name, unit, label, description, kind, first_seen) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_id, name, unit, "", description, kind, ts),
            )
            field_id = cur.lastrowid
            result.new_fields.append(name)
        else:
            field_id = row["id"]

        self._field_cache[(source_id, name)] = field_id
        return field_id

    def _reject(
        self,
        conn: sqlite3.Connection,
        reporter_id: str,
        received_at: str,
        reason: str,
        item: Any,
        result: IngestResult,
    ) -> None:
        result.rejected += 1
        result.reasons.append(reason)
        raw = None
        if isinstance(item, dict) and isinstance(item.get("raw"), str):
            try:
                raw = bytes.fromhex(item["raw"])
            except ValueError:
                raw = None
        conn.execute(
            "INSERT INTO rejects(received_at, reporter_id, reason, raw, data) "
            "VALUES (?,?,?,?,?)",
            (received_at, reporter_id, reason, raw, json.dumps(item, default=str)[:4000]),
        )

    # -- read side (minimal for now; the full read API lands in step 3) ----

    def flights(self) -> list[dict]:
        rows = self.pool.get().execute(
            "SELECT f.*, "
            " (SELECT COUNT(*) FROM sources s WHERE s.flight_id=f.id) AS source_count, "
            " (SELECT COUNT(*) FROM packets p JOIN sources s ON s.id=p.source_id "
            "  WHERE s.flight_id=f.id) AS packet_count, "
            " (SELECT COUNT(*) FROM gps g WHERE g.flight_id=f.id) AS gps_count "
            "FROM flights f ORDER BY f.started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def sources(self, flight_id: int) -> list[dict]:
        conn = self.pool.get()
        rows = conn.execute(
            "SELECT * FROM sources WHERE flight_id=? ORDER BY payload_name, task_name",
            (flight_id,),
        ).fetchall()
        out = []
        for row in rows:
            fields = conn.execute(
                "SELECT id, name, unit, label, description, kind FROM fields "
                "WHERE source_id=? ORDER BY name",
                (row["id"],),
            ).fetchall()
            item = dict(row)
            item["fields"] = [dict(f) for f in fields]
            out.append(item)
        return out

    def reporters(self, flight_id: int) -> list[dict]:
        """Per-callsign reception counts, sensor and GPS together.

        GPS receptions live in their own table because GPS is keyed per flight
        rather than per source. Counting only `receptions` made a station that
        forwards mostly GPS look nearly idle -- which is precisely the station
        carrying the position data.
        """
        rows = self.pool.get().execute(
            "SELECT reporter_id, SUM(packets) AS packets, "
            "       SUM(sensor) AS sensor, SUM(gps) AS gps, "
            "       MIN(first) AS first, MAX(last) AS last FROM ( "
            "  SELECT r.reporter_id, COUNT(*) AS packets, COUNT(*) AS sensor, 0 AS gps, "
            "         MIN(r.received_at) AS first, MAX(r.received_at) AS last "
            "  FROM receptions r JOIN packets p ON p.id=r.packet_id "
            "  JOIN sources s ON s.id=p.source_id WHERE s.flight_id=? "
            "  GROUP BY r.reporter_id "
            "  UNION ALL "
            "  SELECT g.reporter_id, COUNT(*), 0, COUNT(*), "
            "         MIN(g.received_at), MAX(g.received_at) "
            "  FROM gps_receptions g JOIN gps x ON x.id=g.gps_id "
            "  WHERE x.flight_id=? GROUP BY g.reporter_id "
            ") GROUP BY reporter_id ORDER BY packets DESC",
            (flight_id, flight_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_fields(self, flight_id: int, specs: list[str]) -> list[dict]:
        """Turn `field` query parameters into field rows.

        Accepts a numeric field id, `source_key:field_name`, or a bare field
        name (which matches that name across every source, since the same
        sensor name legitimately appears on more than one payload).
        """
        conn = self.pool.get()
        out: list[dict] = []
        seen: set[int] = set()

        for spec in specs:
            if spec.isdigit():
                rows = conn.execute(
                    "SELECT f.*, s.source_key FROM fields f JOIN sources s ON s.id=f.source_id "
                    "WHERE f.id=? AND s.flight_id=?",
                    (int(spec), flight_id),
                ).fetchall()
            elif ":" in spec:
                source_key, _, name = spec.partition(":")
                rows = conn.execute(
                    "SELECT f.*, s.source_key FROM fields f JOIN sources s ON s.id=f.source_id "
                    "WHERE s.flight_id=? AND s.source_key=? AND f.name=?",
                    (flight_id, source_key, name),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT f.*, s.source_key FROM fields f JOIN sources s ON s.id=f.source_id "
                    "WHERE s.flight_id=? AND f.name=? ORDER BY s.source_key",
                    (flight_id, spec),
                ).fetchall()

            for row in rows:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    out.append(dict(row))
        return out

    def series(
        self,
        flight_id: int,
        specs: list[str],
        start: str | None = None,
        end: str | None = None,
        max_points: int = 2000,
        axis: str = "time",
    ) -> list[dict]:
        """Values for one or more fields, downsampled to at most `max_points`.

        Downsampling keeps min and max per bucket rather than sampling every Nth
        point: a spike that a decimating sampler would step over is exactly the
        thing worth seeing in flight data.
        """
        conn = self.pool.get()
        out = []
        for field in self.resolve_fields(flight_id, specs):
            if field["kind"] != "numeric":
                continue
            if axis == "altitude":
                series = self._series_by_altitude(conn, field, max_points, start, end)
            else:
                series = self._series_by_time(conn, field, start, end, max_points)
            series.update(
                field_id=field["id"],
                name=field["name"],
                source=field["source_key"],
                unit=field["unit"],
                label=field["label"] or field["name"],
                axis=axis,
            )
            out.append(series)
        return out

    def _series_by_time(self, conn, field, start, end, max_points) -> dict:
        lo = to_epoch(start) if start else None
        hi = to_epoch(end) if end else None
        where = "field_id=?"
        params: list = [field["id"]]
        if lo is not None:
            where += " AND ts_epoch>=?"
            params.append(lo)
        if hi is not None:
            where += " AND ts_epoch<=?"
            params.append(hi)

        bounds = conn.execute(
            f"SELECT COUNT(*) n, MIN(ts_epoch) lo, MAX(ts_epoch) hi FROM readings WHERE {where}",
            params,
        ).fetchone()
        if not bounds["n"]:
            return {"x": [], "min": [], "max": [], "avg": [], "n": 0, "downsampled": False}

        if bounds["n"] <= max_points:
            rows = conn.execute(
                f"SELECT ts_epoch x, value v FROM readings WHERE {where} ORDER BY ts_epoch",
                params,
            ).fetchall()
            values = [r["v"] for r in rows]
            return {
                "x": [r["x"] for r in rows],
                "min": values, "max": values, "avg": values,
                "n": bounds["n"], "downsampled": False,
            }

        width = max((bounds["hi"] - bounds["lo"]) / max_points, 1e-9)
        rows = conn.execute(
            f"SELECT CAST((ts_epoch-?)/? AS INTEGER) b, AVG(ts_epoch) x, "
            f"MIN(value) lo, MAX(value) hi, AVG(value) avg "
            f"FROM readings WHERE {where} GROUP BY b ORDER BY b",
            [bounds["lo"], width, *params],
        ).fetchall()
        return {
            "x": [r["x"] for r in rows],
            "min": [r["lo"] for r in rows],
            "max": [r["hi"] for r in rows],
            "avg": [r["avg"] for r in rows],
            "n": bounds["n"], "downsampled": True,
        }

    def _series_by_altitude(self, conn, field, max_points, start=None, end=None) -> dict:
        """The plot that ingesting GPS was for: a sensor against height.

        A time window still applies here -- narrowing to the ascent and leaving
        the descent out is exactly how you read a sounding without the two legs
        drawn over each other.
        """
        where = "r.field_id=? AND p.altitude IS NOT NULL"
        params: list = [field["id"]]
        if start:
            where += " AND r.ts_epoch>=?"
            params.append(to_epoch(start))
        if end:
            where += " AND r.ts_epoch<=?"
            params.append(to_epoch(end))

        bounds = conn.execute(
            f"SELECT COUNT(*) n, MIN(p.altitude) lo, MAX(p.altitude) hi "
            f"FROM readings r JOIN packets p ON p.id=r.packet_id WHERE {where}",
            params,
        ).fetchone()
        if not bounds["n"]:
            return {"x": [], "min": [], "max": [], "avg": [], "n": 0, "downsampled": False}

        width = max((bounds["hi"] - bounds["lo"]) / max_points, 1e-9)
        rows = conn.execute(
            f"SELECT CAST((p.altitude-?)/? AS INTEGER) b, AVG(p.altitude) x, "
            f"MIN(r.value) lo, MAX(r.value) hi, AVG(r.value) avg "
            f"FROM readings r JOIN packets p ON p.id=r.packet_id "
            f"WHERE {where} GROUP BY b ORDER BY b",
            [bounds["lo"], width, *params],
        ).fetchall()
        return {
            "x": [r["x"] for r in rows],
            "min": [r["lo"] for r in rows],
            "max": [r["hi"] for r in rows],
            "avg": [r["avg"] for r in rows],
            "n": bounds["n"], "downsampled": bounds["n"] > len(rows),
        }

    def track(
        self,
        flight_id: int,
        max_points: int = 5000,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        """The GPS track, thinned by even stride if it is long."""
        conn = self.pool.get()
        where = "flight_id=? AND latitude IS NOT NULL"
        params: list = [flight_id]
        if start:
            where += " AND time_epoch>=?"
            params.append(to_epoch(start))
        if end:
            where += " AND time_epoch<=?"
            params.append(to_epoch(end))

        total = conn.execute(f"SELECT COUNT(*) FROM gps WHERE {where}", params).fetchone()[0]
        if not total:
            return {"t": [], "lat": [], "lon": [], "alt": [], "ascent_rate": [],
                    "n": 0, "stride": 1}

        stride = max(total // max_points, 1)
        rows = conn.execute(
            f"SELECT timestamp, time_epoch, latitude, longitude, altitude, ascent_rate "
            f"FROM gps WHERE {where} ORDER BY time_epoch",
            params,
        ).fetchall()[::stride]

        return {
            "t": [r["time_epoch"] for r in rows],
            "lat": [r["latitude"] for r in rows],
            "lon": [r["longitude"] for r in rows],
            "alt": [r["altitude"] for r in rows],
            "ascent_rate": [r["ascent_rate"] for r in rows],
            "n": total,
            "stride": stride,
        }

    def latest(self, flight_id: int) -> dict:
        """Newest sample per source plus the current GPS fix -- the live tiles."""
        conn = self.pool.get()
        sources = []
        for src in conn.execute(
            "SELECT * FROM sources WHERE flight_id=? ORDER BY payload_name, task_name",
            (flight_id,),
        ):
            packet = conn.execute(
                "SELECT payload_time, time_epoch, count, data, truncated, altitude, "
                "       first_heard "
                "FROM packets WHERE source_id=? ORDER BY time_epoch DESC LIMIT 1",
                (src["id"],),
            ).fetchone()
            if packet is None:
                continue
            # Rate over the last minute of the source's own clock, so a stalled
            # source reads 0 rather than inheriting the last known rate.
            recent = conn.execute(
                "SELECT COUNT(*) FROM packets WHERE source_id=? AND time_epoch>?",
                (src["id"], packet["time_epoch"] - 60.0),
            ).fetchone()[0]
            sources.append({
                "source_key": src["source_key"],
                "payload_name": src["payload_name"],
                "task_name": src["task_name"],
                "packet_count": src["packet_count"],
                "last_seen": src["last_seen"],
                "time": packet["payload_time"],
                "time_epoch": packet["time_epoch"],
                # When the *server* heard it. `time_epoch` is the payload's own
                # RTC, which is only real time if a PCF8523 was found at boot --
                # otherwise it restarts at 2021-01-01 every power cycle. Judging
                # staleness on that pins a perfectly healthy source to STALLED
                # forever while its values keep updating.
                "heard_epoch": to_epoch(packet["first_heard"]),
                "count": packet["count"],
                "truncated": bool(packet["truncated"]),
                "altitude": packet["altitude"],
                "rate_hz": round(recent / 60.0, 3),
                "data": json.loads(packet["data"]),
            })

        # The last packet that actually carried a position.
        fix = conn.execute(
            "SELECT timestamp, time_epoch, latitude, longitude, altitude, ascent_rate, "
            "ground_speed, heading, num_sv, gps_fix FROM gps "
            "WHERE flight_id=? AND latitude IS NOT NULL ORDER BY time_epoch DESC LIMIT 1",
            (flight_id,),
        ).fetchone()

        # The newest GPS packet of any kind. Without this the display would keep
        # presenting the last known altitude as current after the receiver lost
        # its lock -- stale data shown as live, which is worse than a gap.
        newest = conn.execute(
            "SELECT timestamp, time_epoch, num_sv, gps_fix, latitude "
            "FROM gps WHERE flight_id=? ORDER BY time_epoch DESC LIMIT 1",
            (flight_id,),
        ).fetchone()

        peak = conn.execute(
            "SELECT MAX(altitude) FROM gps WHERE flight_id=?", (flight_id,)
        ).fetchone()[0]

        status = None
        if newest is not None:
            status = {
                "timestamp": newest["timestamp"],
                "time_epoch": newest["time_epoch"],
                "num_sv": newest["num_sv"],
                "gps_fix": newest["gps_fix"],
                "has_position": newest["latitude"] is not None,
                "position_is_stale": newest["latitude"] is None and fix is not None,
            }

        return {
            "flight_id": flight_id,
            # The browser needs this to measure staleness against the same clock
            # the reception times were written on. Its own may be off -- a
            # chase-vehicle tablet is not a reliable timepiece -- and comparing
            # two clocks is the bug this field exists to stop repeating.
            "server_now": to_epoch(iso_now()),
            "sources": sources,
            "gps": dict(fix) if fix else None,
            "gps_status": status,
            "peak_altitude": peak,
        }

    def update_field(self, field_id: int, unit: str | None, label: str | None) -> dict | None:
        conn = self.pool.get()
        with self._write_lock, conn:
            sets, params = [], []
            if unit is not None:
                sets.append("unit=?")
                params.append(unit)
            if label is not None:
                sets.append("label=?")
                params.append(label)
            if sets:
                # Mark it hand-set so seeding can never overwrite it later.
                sets.append("user_edited=1")
                params.append(field_id)
                conn.execute(f"UPDATE fields SET {', '.join(sets)} WHERE id=?", params)
        row = conn.execute("SELECT * FROM fields WHERE id=?", (field_id,)).fetchone()
        return dict(row) if row else None

    # -- flight administration --------------------------------------------

    def update_flight(self, flight_id: int, name: str | None, notes: str | None) -> dict | None:
        conn = self.pool.get()
        with self._write_lock, conn:
            sets, params = [], []
            if name is not None:
                sets.append("name=?")
                params.append(name)
            if notes is not None:
                sets.append("notes=?")
                params.append(notes)
            if sets:
                params.append(flight_id)
                conn.execute(f"UPDATE flights SET {', '.join(sets)} WHERE id=?", params)
        row = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
        return dict(row) if row else None

    def close_flight(self, flight_id: int) -> dict | None:
        """End a flight now rather than waiting for the silence gap to notice."""
        conn = self.pool.get()
        with self._write_lock, conn:
            row = conn.execute("SELECT last_activity FROM flights WHERE id=?",
                               (flight_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE flights SET ended_at=COALESCE(ended_at, ?) WHERE id=?",
                         (row["last_activity"], flight_id))
        self._source_cache.clear()
        self._field_cache.clear()
        # Its tail can be resolved now: nothing further is coming.
        self.backfill_altitudes(flight_id, flush=True)
        return dict(conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone())

    def merge_flights(self, target_id: int, source_id: int) -> dict:
        """Fold one flight into another.

        This is the repair for the failure mode the gap heuristic actually has:
        lose the balloon behind a ridge for half an hour and a single flight is
        recorded as two. Splitting wrongly is easy to do and, without this,
        impossible to undo.
        """
        if target_id == source_id:
            raise ValueError("cannot merge a flight into itself")

        conn = self.pool.get()
        with self._write_lock, conn:
            calls = {}
            for fid in (target_id, source_id):
                row = conn.execute(
                    "SELECT payload_callsign FROM flights WHERE id=?", (fid,)
                ).fetchone()
                if row is None:
                    raise LookupError(f"no flight {fid}")
                calls[fid] = row["payload_callsign"]

            # Merging is for one transmitter's flight that got split, not for
            # gluing two balloons together -- that would put two GPS tracks in
            # one flight and interpolate one balloon's altitude onto the
            # other's sensors. An untagged flight merges freely: that is the
            # cutover case, the same balloon before and after the receivers
            # started naming it.
            if calls[target_id] and calls[source_id] and calls[target_id] != calls[source_id]:
                raise ValueError(
                    f"flights are from different transmitters "
                    f"({calls[target_id]} and {calls[source_id]})"
                )
            merged_callsign = calls[target_id] or calls[source_id]

            moved_sources = 0
            for src in conn.execute("SELECT * FROM sources WHERE flight_id=?",
                                    (source_id,)).fetchall():
                twin = conn.execute(
                    "SELECT * FROM sources WHERE flight_id=? AND source_key=?",
                    (target_id, src["source_key"]),
                ).fetchone()

                if twin is None:
                    conn.execute("UPDATE sources SET flight_id=? WHERE id=?",
                                 (target_id, src["id"]))
                    moved_sources += 1
                    continue

                self._fold_source(conn, src, twin)
                moved_sources += 1

            # GPS is keyed per flight, so drop any instant the target already has
            # before repointing -- the same fix heard twice is one fix.
            conn.execute(
                "DELETE FROM gps WHERE flight_id=? AND timestamp IN "
                "(SELECT timestamp FROM gps WHERE flight_id=?)",
                (source_id, target_id),
            )
            conn.execute("UPDATE gps SET flight_id=? WHERE flight_id=?",
                         (target_id, source_id))

            span = conn.execute(
                "SELECT MIN(started_at) s, MAX(last_activity) a, "
                "       SUM(ended_at IS NULL) open FROM flights WHERE id IN (?,?)",
                (target_id, source_id),
            ).fetchone()
            ended = None if span["open"] else span["a"]
            conn.execute(
                "UPDATE flights SET started_at=?, last_activity=?, ended_at=?, "
                "payload_callsign=? WHERE id=?",
                (span["s"], span["a"], ended, merged_callsign, target_id),
            )
            conn.execute("DELETE FROM flights WHERE id=?", (source_id,))

        self._source_cache.clear()
        self._field_cache.clear()
        self.backfill_altitudes(target_id, force=True)
        log.info("merged flight %s into %s", source_id, target_id)
        return {"flight_id": target_id, "merged_sources": moved_sources}

    def _fold_source(self, conn, src, twin) -> None:
        """Move one source's rows onto an identically-keyed source."""
        for field in conn.execute("SELECT * FROM fields WHERE source_id=?",
                                  (src["id"],)).fetchall():
            existing = conn.execute(
                "SELECT id FROM fields WHERE source_id=? AND name=?",
                (twin["id"], field["name"]),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO fields(source_id, name, unit, label, description, kind, "
                    "user_edited, first_seen) VALUES (?,?,?,?,?,?,?,?)",
                    (twin["id"], field["name"], field["unit"], field["label"],
                     field["description"], field["kind"], field["user_edited"],
                     field["first_seen"]),
                )
                target_field = cur.lastrowid
            else:
                target_field = existing["id"]
            conn.execute("UPDATE readings SET field_id=? WHERE field_id=?",
                         (target_field, field["id"]))
            conn.execute("DELETE FROM fields WHERE id=?", (field["id"],))

        # A packet the target already holds is the same transmission, not a new
        # sample; drop it rather than violating the dedup key on the way in.
        conn.execute(
            "DELETE FROM packets WHERE source_id=? AND (payload_time, count) IN "
            "(SELECT payload_time, count FROM packets WHERE source_id=?)",
            (src["id"], twin["id"]),
        )
        conn.execute("UPDATE packets SET source_id=? WHERE source_id=?",
                     (twin["id"], src["id"]))
        conn.execute(
            "UPDATE sources SET first_seen=MIN(first_seen, ?), last_seen=MAX(last_seen, ?), "
            "packet_count=(SELECT COUNT(*) FROM packets WHERE source_id=?) WHERE id=?",
            (src["first_seen"], src["last_seen"], twin["id"], twin["id"]),
        )
        conn.execute("DELETE FROM sources WHERE id=?", (src["id"],))

    def split_flight(self, flight_id: int, at: str) -> dict:
        """Cut a flight in two at a timestamp; later samples become a new flight."""
        cut = to_epoch(at)
        conn = self.pool.get()

        with self._write_lock, conn:
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
            if flight is None:
                raise LookupError(f"no flight {flight_id}")

            moving = conn.execute(
                "SELECT COUNT(*) FROM packets p JOIN sources s ON s.id=p.source_id "
                "WHERE s.flight_id=? AND p.time_epoch>=?",
                (flight_id, cut),
            ).fetchone()[0]
            if not moving:
                raise ValueError("nothing falls after that time")

            new_id = self._create_flight(conn, iso(at))

            for src in conn.execute("SELECT * FROM sources WHERE flight_id=?",
                                    (flight_id,)).fetchall():
                later = conn.execute(
                    "SELECT COUNT(*) FROM packets WHERE source_id=? AND time_epoch>=?",
                    (src["id"], cut),
                ).fetchone()[0]
                if not later:
                    continue

                cur = conn.execute(
                    "INSERT INTO sources(flight_id, source_key, payload_name, task_name, "
                    "first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                    (new_id, src["source_key"], src["payload_name"], src["task_name"],
                     iso(at), src["last_seen"]),
                )
                new_src = cur.lastrowid

                # Readings hang off fields, and fields hang off a source, so the
                # fields have to be cloned before the packets can move.
                for field in conn.execute("SELECT * FROM fields WHERE source_id=?",
                                          (src["id"],)).fetchall():
                    cur = conn.execute(
                        "INSERT INTO fields(source_id, name, unit, label, description, "
                        "kind, user_edited, first_seen) VALUES (?,?,?,?,?,?,?,?)",
                        (new_src, field["name"], field["unit"], field["label"],
                         field["description"], field["kind"], field["user_edited"],
                         field["first_seen"]),
                    )
                    conn.execute(
                        "UPDATE readings SET field_id=? WHERE field_id=? AND ts_epoch>=?",
                        (cur.lastrowid, field["id"], cut),
                    )

                conn.execute(
                    "UPDATE packets SET source_id=? WHERE source_id=? AND time_epoch>=?",
                    (new_src, src["id"], cut),
                )
                for sid in (src["id"], new_src):
                    conn.execute(
                        "UPDATE sources SET packet_count="
                        "(SELECT COUNT(*) FROM packets WHERE source_id=?) WHERE id=?",
                        (sid, sid),
                    )

            conn.execute("UPDATE gps SET flight_id=? WHERE flight_id=? AND time_epoch>=?",
                         (new_id, flight_id, cut))

            # Sources with nothing left behind the cut are an artefact, not data.
            conn.execute(
                "DELETE FROM sources WHERE flight_id=? AND packet_count=0", (flight_id,)
            )
            conn.execute(
                "UPDATE flights SET ended_at=?, last_activity=? WHERE id=?",
                (iso(at), iso(at), flight_id),
            )

        self._source_cache.clear()
        self._field_cache.clear()
        self.backfill_altitudes(flight_id, force=True)
        self.backfill_altitudes(new_id, force=True)
        log.info("split flight %s at %s -> new flight %s", flight_id, at, new_id)
        return {"flight_id": flight_id, "new_flight_id": new_id, "moved_packets": moving}

    def stats(self) -> dict:
        conn = self.pool.get()

        def scalar(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        return {
            "flights": scalar("SELECT COUNT(*) FROM flights"),
            "sources": scalar("SELECT COUNT(*) FROM sources"),
            "packets": scalar("SELECT COUNT(*) FROM packets"),
            "receptions": scalar("SELECT COUNT(*) FROM receptions"),
            "readings": scalar("SELECT COUNT(*) FROM readings"),
            "gps": scalar("SELECT COUNT(*) FROM gps"),
            "rejects": scalar("SELECT COUNT(*) FROM rejects"),
        }
