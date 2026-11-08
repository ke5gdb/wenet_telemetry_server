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
import math
import re
import sqlite3
import threading
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

from . import decode as cbor_decode
from .db import ConnectionPool, init_db
from .timeutil import iso, iso_now, parse_ts, to_epoch
from .units import RESERVED, field_meta

log = logging.getLogger(__name__)

#: A GPS telemetry packet is a sensor packet that happens to carry a position.
#: Besides the fix it reports the transmitter's own health -- CPU and radio
#: temperature, clock, load, disk, battery volts and amps, camera focus -- and
#: none of that is visible if it is only ever filed as a map point. So the same
#: packet is also stored as a source, with fields and readings, exactly like a
#: CBOR packet from a BLE payload: chartable, exportable, and in the source
#: list beside everything else.
#:
#: These keys are the exception, describing the packet rather than measuring
#: anything. The week/second pair is the timestamp in its raw form and would
#: plot as a sawtooth; `time_source` is the server's own note about which clock
#: was used; `error` is the decoder's status, and is None on every packet that
#: got this far.
_GPS_NOT_A_SERIES = frozenset({
    "timestamp", "time", "week", "iTOW", "leapS", "error", "time_source",
})

#: Task name for that synthesised source. The payload name is the transmitter's
#: callsign where the receiver gave one.
_GPS_TASK = "GPS"

#: How many packets the reported rate is measured over.
#:
#: A count of packets in a fixed 60 s window was the obvious thing and the wrong
#: one: sources here run anywhere from 0.05 Hz to 2 Hz, and one window cannot
#: serve both. At 2 Hz it averages away a minute of detail; at 0.05 Hz it counts
#: three packets and quantises the answer to a third of itself.
#:
#: Measuring over a fixed number of packets is scale-free -- the window becomes
#: however long those packets took -- so the same constant reads a fast source
#: and a slow one equally well. 20 is a compromise: ~10 s at 2 Hz, responsive
#: enough to show a link degrading, long enough that one dropped packet moves
#: the figure by 5% rather than 10%.
RATE_SAMPLES = 20


def rate_from_epochs(epochs: list[float]) -> float:
    """Packets per second across a run of packets, newest first.

    Intervals, not a count over a window: `n` packets span `n-1` gaps, and
    dividing by the elapsed time between the first and last is the rate at which
    they actually arrived. Losses are in the answer rather than smoothed out of
    it -- a display of received rate should say when half of them stopped
    arriving.
    """
    if len(epochs) < 2:
        return 0.0
    span = epochs[0] - epochs[-1]
    # Packets are unique on (source, time, count), so several can legitimately
    # share a timestamp -- a truncated packet loses its count and a payload with
    # a coarse clock can emit two in the same second. No span, no rate.
    if span <= 0:
        return 0.0
    return (len(epochs) - 1) / span

#: How far an untagged GPS fix may be from a flight's latest fix and still be
#: filed with it. This only has to tell apart balloons that are plainly not
#: the same one: within a 30-minute flight gap a balloon in a 50 m/s jet stream
#: covers ~90 km, while an unrelated transmitter is usually on another
#: continent. Where two flights are both in reach, the nearer one wins anyway.
ATTRIBUTION_MAX_KM = 100.0

#: A name `_create_flight` generated, as opposed to one a person chose. Only
#: these are rewritten when a flight learns its callsign.
_AUTO_NAME = re.compile(r"^Flight \d{4}-\d{2}-\d{2}( - \S+)? #\d+$")


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


@dataclass(frozen=True)
class Evidence:
    """What a packet says about which flight it belongs to, besides a callsign.

    Needed because the callsign is frequently missing. The Wenet receiver
    learns it from SSDV image packets, so everything it hears before the first
    image decodes goes out untagged -- and a receiver on a build older than the
    callsign passthrough never tags anything at all.
    """
    source_key: str | None = None
    position: tuple[float, float] | None = None


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

        flight_id = self._current_flight(
            conn, received_at, callsign, Evidence(source_key=source_key)
        )
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
            if value is None:
                # Absent, not textual. A field's kind is fixed when it is first
                # discovered and never upgraded, so registering a null as text
                # would make it text for the life of the flight -- and a GPS
                # packet taken before the receiver locked carries null
                # latitude, longitude and altitude, which is precisely the
                # first packet of most flights.
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

        lat, lon = values.get("latitude"), values.get("longitude")
        position = None
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)):
            position = (float(lat), float(lon))

        flight_id = self._current_flight(
            conn, received_at, callsign, Evidence(position=position)
        )
        result.flight_id = flight_id
        result.flight_ids.add(flight_id)

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
            # Only for the station that got here first. The others are receive
            # diversity, recorded below -- storing the series once is the same
            # rule the sensor path follows.
            self._store_gps_telemetry(
                conn, flight_id, callsign, data, ts, received_at, result
            )
        else:
            gps_id = conn.execute(
                "SELECT id FROM gps WHERE flight_id=? AND timestamp=?", (flight_id, ts)
            ).fetchone()[0]
            result.gps_duplicates += 1

        # Deliberately `gps_receptions` and not `receptions`: the reporters
        # tally unions both tables, so a row in each would count every GPS
        # reception twice, once as sensor and once as GPS.
        conn.execute(
            "INSERT OR IGNORE INTO gps_receptions(gps_id, reporter_id, received_at) "
            "VALUES (?,?,?)",
            (gps_id, reporter_id, received_at),
        )

    def gps_source_key(self, callsign: str | None) -> str:
        """Key for the synthesised source carrying GPS telemetry as series.

        Qualified with the flight's callsign where it has one, which is safe
        because a flight *is* one transmitter. The one change it can undergo is
        an untagged flight learning its callsign, and `_adopt_gps_source`
        renames the key when that happens.
        """
        return f"{callsign}_{_GPS_TASK}" if callsign else f"WENET_{_GPS_TASK}"

    def _store_gps_telemetry(
        self,
        conn: sqlite3.Connection,
        flight_id: int,
        callsign: str | None,
        data: dict,
        ts: str,
        received_at: str,
        result: IngestResult,
    ) -> bool:
        """File a GPS packet a second time, as a source with chartable fields.

        The fix itself is already in `gps`, which is what the map and the
        altitude backfill read. This is the rest of the packet -- the
        transmitter's own housekeeping -- which has nowhere else to live: it
        would otherwise sit in the `extra` JSON blob, stored but unplottable
        and invisible in the source list.
        """
        series = {k: v for k, v in data.items() if k not in _GPS_NOT_A_SERIES}
        if not series:
            return False

        # Keyed on the flight's callsign, not the packet's. `callsign` is what
        # this one receiver reported, and may be absent even though the flight
        # is known to be KE5GDB's -- keying on it would give one transmitter two
        # telemetry sources, `KE5GDB_GPS` and `WENET_GPS`, splitting its
        # housekeeping across both. The packet row below still records the raw
        # fact.
        flight_callsign = self._flight_callsign(conn, flight_id)
        source_key = self.gps_source_key(flight_callsign)
        source_id = self._get_source(
            conn, flight_id, source_key, data, ts, result,
            names=(flight_callsign or "Wenet", _GPS_TASK), kind="gps",
        )

        # This packet *is* a fix, so its altitude needs no interpolating -- it
        # is resolved on arrival, against itself. A packet that came in without
        # one is left pending instead, so the backfill can reach it from the
        # surrounding track: "no fix here" is exactly when plotting the
        # transmitter's housekeeping against altitude still matters.
        own_altitude = data.get("altitude")
        if not isinstance(own_altitude, (int, float)) or isinstance(own_altitude, bool):
            own_altitude = None

        # `count` is a sensor-packet concept; GPS has no sequence number, so the
        # dedup key falls back to (source, time), which is exactly the key the
        # `gps` table already enforces.
        cur = conn.execute(
            "INSERT OR IGNORE INTO packets"
            "(source_id, payload_time, count, time_epoch, data, raw_cbor, truncated,"
            " first_heard, payload_callsign, altitude, altitude_resolved) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id, ts, -1, to_epoch(ts), json.dumps(data, default=str),
                None, 0, received_at, callsign,
                own_altitude, 1 if own_altitude is not None else 0,
            ),
        )
        if not cur.rowcount:
            return False

        self._store_readings(conn, source_id, cur.lastrowid, ts, series, result)
        conn.execute(
            "UPDATE sources SET last_seen=MAX(last_seen, ?), "
            "packet_count=packet_count+1 WHERE id=?",
            (ts, source_id),
        )
        previous = result.latest.get(source_key)
        if previous is None or ts > previous["time"]:
            result.latest[source_key] = {
                "time": ts,
                "count": -1,
                "truncated": False,
                "data": series,
                "flight_id": flight_id,
            }
        return True

    #: gps table column -> the key the packet arrived with, for reconstructing a
    #: decoded packet from a stored row.
    _GPS_ROW_KEYS = {
        "latitude": "latitude", "longitude": "longitude", "altitude": "altitude",
        "ascent_rate": "ascent_rate", "ground_speed": "ground_speed",
        "heading": "heading", "num_sv": "numSV", "gps_fix": "gpsFix",
    }

    def materialize_gps_telemetry(self, flight_id: int) -> dict:
        """Build the GPS telemetry source for a flight already in the store.

        For databases written before GPS packets were filed as a source: the
        housekeeping is all there, sitting in the `extra` blob where nothing can
        chart it. Idempotent -- the packet insert is keyed on (source, time), so
        re-running adds nothing and a partially-done flight finishes cleanly.
        """
        conn = self.pool.get()
        result = IngestResult()
        created = 0

        with self._write_lock, conn:
            rows = conn.execute(
                "SELECT * FROM gps WHERE flight_id=? ORDER BY time_epoch", (flight_id,)
            ).fetchall()

            for row in rows:
                try:
                    extra = json.loads(row["extra"] or "{}")
                except ValueError:
                    extra = {}
                data = {k: v for k, v in extra.items() if k != "time_source"}
                for column, key in self._GPS_ROW_KEYS.items():
                    if row[column] is not None:
                        data[key] = row[column]

                if self._store_gps_telemetry(
                    conn, flight_id, row["payload_callsign"], data,
                    row["timestamp"], row["first_heard"], result,
                ):
                    created += 1

        self._source_cache.clear()
        self._field_cache.clear()
        log.info("flight %s: materialised %d GPS telemetry packets", flight_id, created)
        return {
            "flight_id": flight_id,
            "gps_rows": len(rows),
            "packets_created": created,
            "fields": sorted(set(result.new_fields)),
        }

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
                # Not the GPS source: its altitudes are its own measurements,
                # not interpolations, so clearing them would throw away the one
                # figure that never needed reconsidering -- and the last packet,
                # with no fix past it, would never be resolved again.
                conn.execute(
                    "UPDATE packets SET altitude=NULL, altitude_resolved=0 "
                    "WHERE source_id IN (SELECT id FROM sources "
                    "WHERE flight_id=? AND kind != 'gps')",
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
        self,
        conn: sqlite3.Connection,
        now: str,
        callsign: str | None = None,
        evidence: Evidence | None = None,
    ) -> int:
        """The open flight for one transmitter, or a new one.

        Keyed on the callsign as well as the clock, because a flight is one
        Wenet transmitter's up-burst-down. Any number of BLE sensor payloads
        may be talking to that transmitter and any number of sensors may sit on
        each of them -- those are sources *within* the flight, not flights of
        their own. What must not share a flight is two transmitters, whose
        times overlap freely and whose GPS streams are two different balloons.

        A missing callsign is *not* its own identity. It is what a receiver
        sends before it has decoded an image, and all an older receiver ever
        sends, so treating it as a separate transmitter gave every flight an
        untagged twin running alongside it with part of its telemetry. An
        untagged packet joins the tagged flight it evidently belongs to, and
        the first tagged packet claims an untagged flight already under way.
        Only when nothing can tell which tagged flight an untagged packet is
        from does it stay in an untagged flight of its own.

        ``IS`` rather than ``=`` so the no-callsign case matches itself: in
        SQLite ``NULL = NULL`` is NULL, which would open a fresh flight for
        every untagged packet.
        """
        evidence = evidence or Evidence()
        if callsign is None:
            owner = self._attribute_untagged(conn, now, evidence)
            if owner is not None:
                return owner

        # At most one row, by the unique index on open flights.
        row = conn.execute(
            "SELECT id, last_activity FROM flights "
            "WHERE ended_at IS NULL AND payload_callsign IS ?",
            (callsign,),
        ).fetchone()

        if row is not None:
            if self._gap(now, row["last_activity"]) <= self.flight_gap_seconds:
                return row["id"]
            # Silence long enough to call it: close the old flight at its last
            # real activity rather than now, so the record does not claim hours
            # of flight time that were actually silence.
            self._end_flight(conn, row["id"], row["last_activity"])

        if callsign is not None:
            claimed = self._claim_untagged(conn, now, callsign, evidence)
            if claimed is not None:
                return claimed

        return self._create_flight(conn, now, callsign)

    @staticmethod
    def _gap(now: str, last_activity: str) -> float:
        return (parse_ts(now) - parse_ts(last_activity)).total_seconds()

    def _live_flights(self, conn: sqlite3.Connection, now: str) -> list[sqlite3.Row]:
        """Open flights that `now` is still within the gap of, newest first."""
        rows = conn.execute(
            "SELECT id, name, payload_callsign, started_at, last_activity FROM flights "
            "WHERE ended_at IS NULL ORDER BY last_activity DESC"
        ).fetchall()
        return [r for r in rows
                if self._gap(now, r["last_activity"]) <= self.flight_gap_seconds]

    def _last_fix(self, conn: sqlite3.Connection, flight_id: int) -> tuple[float, float] | None:
        row = conn.execute(
            "SELECT latitude, longitude FROM gps WHERE flight_id=? "
            "AND latitude IS NOT NULL AND longitude IS NOT NULL "
            "ORDER BY time_epoch DESC LIMIT 1",
            (flight_id,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def _attribute_untagged(
        self, conn: sqlite3.Connection, now: str, evidence: Evidence
    ) -> int | None:
        """The tagged flight an untagged packet belongs to, or None to keep it apart.

        In order of how much the packet gives away:

        1. Its source already exists in a live flight. A sensor payload is on
           one balloon, so that is the flight -- unless the source is in the
           untagged flight only, which already says where it goes.
        2. It is a fix. It goes with the nearest live flight, if that one is
           tagged and within `ATTRIBUTION_MAX_KM`.
        3. Otherwise, if exactly one tagged flight is live, it is that one. This
           is the ordinary case -- one balloon up, some receivers too old to
           name it -- and the one that was producing the twin flights.

        GPS timestamps are deliberately not evidence: every transmitter's fixes
        land on whole GPS seconds, so two balloons share them constantly.
        """
        live = self._live_flights(conn, now)
        tagged = [r["id"] for r in live if r["payload_callsign"] is not None]
        if not tagged:
            return None
        ids = [r["id"] for r in live]
        marks = ",".join("?" * len(ids))

        if evidence.source_key is not None:
            holders = {r[0] for r in conn.execute(
                f"SELECT flight_id FROM sources WHERE source_key=? AND flight_id IN ({marks})",
                (evidence.source_key, *ids),
            )}
            for fid in tagged:              # newest activity first
                if fid in holders:
                    return fid
            if holders:
                return None

        if evidence.position is not None:
            reach = []
            for fid in ids:
                fix = self._last_fix(conn, fid)
                if fix is not None:
                    reach.append((distance_km(evidence.position, fix), fid))
            if reach:
                km, nearest = min(reach)
                if nearest in tagged and km <= ATTRIBUTION_MAX_KM:
                    return nearest
                return None

        return tagged[0] if len(tagged) == 1 else None

    def _claim_untagged(
        self, conn: sqlite3.Connection, now: str, callsign: str, evidence: Evidence
    ) -> int | None:
        """Give a live untagged flight to the first callsign heard, if it fits.

        The untagged flight is usually this transmitter's own first minutes,
        heard before any receiver had decoded an image to learn the callsign
        from. The one thing that rules it out is a fix that is plainly
        somewhere else.
        """
        row = conn.execute(
            "SELECT id, name, started_at, last_activity FROM flights "
            "WHERE ended_at IS NULL AND payload_callsign IS NULL"
        ).fetchone()
        if row is None or self._gap(now, row["last_activity"]) > self.flight_gap_seconds:
            return None
        if evidence.position is not None:
            fix = self._last_fix(conn, row["id"])
            if fix is not None and distance_km(evidence.position, fix) > ATTRIBUTION_MAX_KM:
                return None
        self._assign_callsign(conn, row["id"], callsign)
        log.info("flight %s claimed by %s", row["id"], callsign)
        return row["id"]

    def _assign_callsign(self, conn: sqlite3.Connection, flight_id: int, callsign: str) -> None:
        """Tag a flight with its transmitter, renaming what was named for it.

        The caller guarantees no other open flight carries `callsign`.
        """
        row = conn.execute(
            "SELECT name, started_at FROM flights WHERE id=?", (flight_id,)
        ).fetchone()
        name = row["name"]
        if _AUTO_NAME.match(name):
            name = self._flight_name(conn, row["started_at"], callsign)
        conn.execute(
            "UPDATE flights SET payload_callsign=?, name=? WHERE id=?",
            (callsign, name, flight_id),
        )
        self._adopt_gps_source(conn, flight_id, callsign)

    def _adopt_gps_source(self, conn: sqlite3.Connection, flight_id: int, callsign: str) -> None:
        """Re-key a flight's untagged GPS telemetry source to its callsign."""
        old = conn.execute(
            "SELECT * FROM sources WHERE flight_id=? AND source_key=?",
            (flight_id, self.gps_source_key(None)),
        ).fetchone()
        if old is None:
            return
        twin = conn.execute(
            "SELECT * FROM sources WHERE flight_id=? AND source_key=?",
            (flight_id, self.gps_source_key(callsign)),
        ).fetchone()
        if twin is None:
            conn.execute(
                "UPDATE sources SET source_key=?, payload_name=? WHERE id=?",
                (self.gps_source_key(callsign), callsign, old["id"]),
            )
        else:
            self._fold_source(conn, old, twin)
        self._source_cache.clear()
        self._field_cache.clear()

    def _flight_callsign(self, conn: sqlite3.Connection, flight_id: int) -> str | None:
        row = conn.execute(
            "SELECT payload_callsign FROM flights WHERE id=?", (flight_id,)
        ).fetchone()
        return row[0] if row else None

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
        for row in conn.execute(
            "SELECT id, last_activity FROM flights WHERE ended_at IS NULL"
        ).fetchall():
            if self._gap(now, row["last_activity"]) > self.flight_gap_seconds:
                self._end_flight(conn, row["id"], row["last_activity"])

    @staticmethod
    def _flight_name(conn: sqlite3.Connection, started_at: str, callsign: str | None) -> str:
        # "Flight 2026-08-15 - KE5GDB #1". Counted per transmitter per day
        # rather than per day, so two transmitters flying the same afternoon are
        # each their own #1 instead of one of them being arbitrarily "#2".
        # Numbered from the first, because "#1" reads as a flight of several and
        # a bare name reads as the only one -- and on a launch day it is rarely
        # the only one.
        day = started_at[:10]
        base = f"Flight {day} - {callsign}" if callsign else f"Flight {day}"
        same = conn.execute(
            "SELECT COUNT(*) FROM flights WHERE name = ? OR name LIKE ?",
            (base, base + " #%"),
        ).fetchone()[0]
        return f"{base} #{same + 1}"

    def _create_flight(
        self, conn: sqlite3.Connection, now: str, callsign: str | None = None
    ) -> int:
        name = self._flight_name(conn, now, callsign)
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
        names: tuple[str, str] | None = None,
        kind: str = "sensor",
    ) -> int:
        cached = self._source_cache.get((flight_id, source_key))
        if cached is not None:
            return cached

        row = conn.execute(
            "SELECT id FROM sources WHERE flight_id=? AND source_key=?",
            (flight_id, source_key),
        ).fetchone()

        if row is None:
            # `names` is for the synthesised GPS source, whose identity comes
            # from the transmitter rather than from anything inside the packet.
            payload_name, task_name = names or derive_names(source_key, data)
            cur = conn.execute(
                "INSERT INTO sources"
                "(flight_id, source_key, payload_name, task_name, first_seen, last_seen,"
                " kind) VALUES (?,?,?,?,?,?,?)",
                (flight_id, source_key, payload_name, task_name, payload_time,
                 payload_time, kind),
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

    def flights(self, include_hidden: bool = False) -> list[dict]:
        """Every flight, newest first. Hidden ones only when asked for.

        Hiding is a tidiness feature, not access control: a hidden flight's
        data is still served to anyone who asks for it by id.
        """
        rows = self.pool.get().execute(
            "SELECT f.*, "
            " (SELECT COUNT(*) FROM sources s WHERE s.flight_id=f.id) AS source_count, "
            " (SELECT COUNT(*) FROM packets p JOIN sources s ON s.id=p.source_id "
            "  WHERE s.flight_id=f.id) AS packet_count, "
            " (SELECT COUNT(*) FROM gps g WHERE g.flight_id=f.id) AS gps_count "
            "FROM flights f "
            + ("" if include_hidden else "WHERE f.hidden=0 ")
            + "ORDER BY f.started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def flight(self, flight_id: int) -> dict | None:
        row = self.pool.get().execute(
            "SELECT * FROM flights WHERE id=?", (flight_id,)
        ).fetchone()
        return dict(row) if row else None

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
            # One query for both the newest packet and the rate: the rate is
            # measured over the newest few, so they are the same rows.
            recent = conn.execute(
                "SELECT payload_time, time_epoch, count, data, truncated, altitude, "
                "       first_heard "
                "FROM packets WHERE source_id=? ORDER BY time_epoch DESC LIMIT ?",
                (src["id"], RATE_SAMPLES),
            ).fetchall()
            if not recent:
                continue
            packet = recent[0]
            # Measured on the source's own clock. Wall-clock would read zero
            # forever on a payload whose RTC restarts at 2021-01-01 for want of
            # a PCF8523 -- and that source is perfectly healthy. Whether it is
            # still transmitting is a separate question, answered by
            # `heard_epoch` against the server clock.
            rate_hz = rate_from_epochs([row["time_epoch"] for row in recent])
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
                "rate_hz": round(rate_hz, 3),
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

    def update_flight(
        self,
        flight_id: int,
        name: str | None = None,
        notes: str | None = None,
        hidden: bool | None = None,
    ) -> dict | None:
        conn = self.pool.get()
        with self._write_lock, conn:
            sets, params = [], []
            if name is not None:
                sets.append("name=?")
                params.append(name)
            if notes is not None:
                sets.append("notes=?")
                params.append(notes)
            if hidden is not None:
                sets.append("hidden=?")
                params.append(int(hidden))
            if sets:
                params.append(flight_id)
                conn.execute(f"UPDATE flights SET {', '.join(sets)} WHERE id=?", params)
            row = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
        if row is None:
            return None
        # A hidden flight that stayed open would keep taking packets -- so a
        # test flight hidden on the pad would swallow the real launch where no
        # one can see it. Closing it means the next packet opens a fresh one.
        if hidden and row["ended_at"] is None:
            return self.close_flight(flight_id)
        return dict(row)

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
            # before repointing -- the same fix heard twice is one fix. Who
            # heard it is kept: those are two stations' receptions of it, and
            # the reporters panel is the only record of the second one.
            conn.execute(
                "INSERT OR IGNORE INTO gps_receptions(gps_id, reporter_id, received_at) "
                "SELECT t.id, r.reporter_id, r.received_at FROM gps s "
                "JOIN gps t ON t.flight_id=? AND t.timestamp=s.timestamp "
                "JOIN gps_receptions r ON r.gps_id=s.id WHERE s.flight_id=?",
                (target_id, source_id),
            )
            conn.execute(
                "DELETE FROM gps WHERE flight_id=? AND timestamp IN "
                "(SELECT timestamp FROM gps WHERE flight_id=?)",
                (source_id, target_id),
            )
            conn.execute("UPDATE gps SET flight_id=? WHERE flight_id=?",
                         (target_id, source_id))

            span = conn.execute(
                "SELECT MIN(started_at) s, MAX(last_activity) a, "
                "       SUM(ended_at IS NULL) open, MIN(hidden) hidden "
                "FROM flights WHERE id IN (?,?)",
                (target_id, source_id),
            ).fetchone()
            ended = None if span["open"] else span["a"]
            if ended is None:
                clash = conn.execute(
                    "SELECT id FROM flights WHERE ended_at IS NULL AND id NOT IN (?,?) "
                    "AND payload_callsign IS ?",
                    (target_id, source_id, merged_callsign),
                ).fetchone()
                if clash is not None:
                    raise ValueError(
                        f"flight {clash['id']} is already open for "
                        f"{merged_callsign or 'untagged traffic'}; close it first"
                    )
            # The source goes first: until it does, it may be the one other
            # open flight for this callsign, and the unique index checks each
            # statement as it runs rather than at commit.
            conn.execute("DELETE FROM flights WHERE id=?", (source_id,))
            conn.execute(
                "UPDATE flights SET started_at=?, last_activity=?, ended_at=?, hidden=? "
                "WHERE id=?",
                (span["s"], span["a"], ended, span["hidden"], target_id),
            )
            if merged_callsign and calls[target_id] is None:
                self._assign_callsign(conn, target_id, merged_callsign)
            elif merged_callsign:
                # Its untagged twin brought a `WENET_GPS` source with it.
                self._adopt_gps_source(conn, target_id, merged_callsign)

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
        # sample; drop it rather than violating the dedup key on the way in --
        # but keep who heard it, which is the whole of what the copy adds.
        conn.execute(
            "INSERT OR IGNORE INTO receptions(packet_id, reporter_id, received_at) "
            "SELECT t.id, r.reporter_id, r.received_at FROM packets s "
            "JOIN packets t ON t.source_id=? AND t.payload_time=s.payload_time "
            "AND t.count=s.count "
            "JOIN receptions r ON r.packet_id=s.id WHERE s.source_id=?",
            (twin["id"], src["id"]),
        )
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

            # The earlier half is closed at the cut first, and the later half
            # inherits the flight's state: if it was open it still is, and
            # ending the original before creating the new one keeps the two
            # from ever both being open for the same callsign.
            conn.execute(
                "UPDATE flights SET ended_at=?, last_activity=? WHERE id=?",
                (iso(at), iso(at), flight_id),
            )
            new_id = self._create_flight(conn, iso(at), flight["payload_callsign"])
            conn.execute(
                "UPDATE flights SET last_activity=?, ended_at=?, hidden=? WHERE id=?",
                (flight["last_activity"], flight["ended_at"], flight["hidden"], new_id),
            )

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
                    "first_seen, last_seen, kind) VALUES (?,?,?,?,?,?,?)",
                    (new_id, src["source_key"], src["payload_name"], src["task_name"],
                     iso(at), src["last_seen"], src["kind"]),
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
