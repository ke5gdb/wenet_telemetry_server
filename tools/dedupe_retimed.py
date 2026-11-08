#!/usr/bin/env python3
"""Repair a flight whose samples were sent several times with different clocks.

The dedup key is (source, payload_time, count), and `time` is written on the
balloon by the BLE bridge. A balloon running more than one bridge sends every
sample several times, each copy timestamped by its own bridge, so they never
match on that key. On 2026-10-24 the BLN_NECK payload was relayed by three:

  - two current bridges, re-timing each sample from the host clock, 3 ms apart
    -- and for 13 minutes after boot that clock was 5 hours fast (UTC/CDT);
  - one pre-2026-08-14 bridge forwarding the Pico's own RTC, 544 s fast, in
    legacy 0x03 frames padded with zeros to the end of the Wenet frame.

Plotted by time, the stale copies sat among samples nine minutes newer -- a
monotonic counter like `fall_cnt` read as oscillating -- and the 5-hour ones
stretched the flight's time axis hours past landing.

**Copies of one sample** are packets from the same source with the same
`count`, identical in every field but `time`, first heard within --window
seconds of each other. One is kept, preferring in order: a time that is not
after the packet was heard (a sample cannot be stamped later than it arrived),
an exactly-sized 0x05 copy over a padded legacy one, the earliest time. Every
station's reception of a dropped copy moves onto the one kept.

**Re-timing.** Where every copy of a sample is wrong, the kept one is moved to
the host-clock time it would have had. The legacy clock's error is measured
from samples that arrived both ways -- legacy time minus host time, steady at
544.0-544.7 s on that flight -- and the median of the nearest pairs by `count`
is subtracted from the legacy copy's time. `raw_cbor` is never touched, so the
original bytes remain the record; `data.time`, the indexed time and the
readings move, and altitudes are re-resolved.

Dry run by default; pass --apply to write.

    python tools/dedupe_retimed.py --flight 19
    python tools/dedupe_retimed.py --flight 19 --apply
"""

from __future__ import annotations

import argparse
import bisect
import io
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import cbor2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: A time this far past the packet's first reception cannot be its sample time.
FUTURE_SLACK_S = 5.0
#: Pairs either side of a sample used to estimate the legacy clock's error.
OFFSET_NEIGHBOURS = 9
#: Further than this in arrival time from any measured pair, no offset is trusted.
#: Neighbours are found by arrival, not `count`: the counter restarts when the
#: payload reboots (4240 -> 10 at 13:50:52 on 2026-10-24), so one count number
#: can belong to two runs, while arrival time only moves forward.
OFFSET_MAX_GAP_S = 1800.0


def padded(raw: bytes | None) -> bool:
    """True when bytes follow the CBOR map -- the frame filler a 0x03 copy carries."""
    if not raw:
        return False
    stream = io.BytesIO(raw)
    try:
        cbor2.CBORDecoder(stream).decode()
    except Exception:
        return False
    return stream.tell() < len(raw)


def epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def wire_time(t: float) -> str:
    """The firmware's own format, for `data.time`."""
    d = datetime.fromtimestamp(t, timezone.utc)
    return f"{d.strftime('%Y%m%dT%H%M%S')}.{d.microsecond // 1000:03d}Z"


def plan_source(conn: sqlite3.Connection, source_id: int, window: float) -> dict:
    """Decide what to keep, drop and re-time for one source, without writing."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT id, count, payload_time, time_epoch, first_heard, data, raw_cbor FROM packets "
        "WHERE source_id=? AND count >= 0", (source_id,)
    ):
        body = json.loads(r["data"])
        body.pop("time", None)
        heard = epoch(r["first_heard"])
        groups[(r["count"], json.dumps(body, sort_keys=True))].append({
            "id": r["id"], "count": r["count"], "t": r["time_epoch"], "heard": heard,
            "time": r["payload_time"],
            "padded": padded(r["raw_cbor"]),
            "future": r["time_epoch"] > heard + FUTURE_SLACK_S,
        })

    # Same count and same readings, but far apart in arrival: a counter that
    # wrapped or restarted, not a re-transmission. Kept as separate samples.
    samples: list[list[dict]] = []
    for copies in groups.values():
        copies.sort(key=lambda c: c["heard"])
        current = [copies[0]]
        for c in copies[1:]:
            if c["heard"] - current[0]["heard"] <= window:
                current.append(c)
            else:
                samples.append(current)
                current = [c]
        samples.append(current)

    # The legacy clock's error, from samples that arrived both ways, keyed on
    # when they arrived.
    pairs = []
    for copies in samples:
        legacy = [c["t"] for c in copies if c["padded"]]
        good = [c["t"] for c in copies if not c["padded"] and not c["future"]]
        if legacy and good:
            pairs.append((copies[0]["heard"], min(legacy) - min(good)))
    pairs.sort()
    pair_times = [p[0] for p in pairs]

    def legacy_offset(heard: float) -> float | None:
        i = bisect.bisect_left(pair_times, heard)
        near = pairs[max(0, i - OFFSET_NEIGHBOURS):i + OFFSET_NEIGHBOURS]
        near = [p for p in near if abs(p[0] - heard) <= OFFSET_MAX_GAP_S]
        return statistics.median(p[1] for p in near) if near else None

    drops: list[tuple[int, int]] = []        # (dropped id, kept id)
    retime: list[tuple[int, float]] = []     # (kept id, corrected epoch)
    stats = defaultdict(int)
    # Kept copies whose time stands, by (count, time) -- where a re-timed copy
    # landing on one of these is the same sample that the window kept apart.
    standing: dict[tuple[int, str], int] = {}
    for copies in samples:
        keep = min(copies, key=lambda c: (c["future"], c["padded"], c["t"]))
        if not keep["padded"] and not keep["future"]:
            standing[(keep["count"], keep["time"])] = keep["id"]

    for copies in samples:
        copies.sort(key=lambda c: (c["future"], c["padded"], c["t"]))
        keep = copies[0]
        for dup in copies[1:]:
            drops.append((dup["id"], keep["id"]))
            stats["drop_legacy" if dup["padded"] else
                  "drop_future" if dup["future"] else "drop_twin"] += 1

        if not keep["padded"] and not keep["future"]:
            continue
        # Every copy is wrong. A legacy copy is re-timed from the measured
        # error; a future host-clock copy from its legacy sibling, if any.
        legacy = [c for c in copies if c["padded"]]
        offset = legacy_offset(copies[0]["heard"]) if legacy else None
        if offset is None:
            stats["unfixable"] += 1
            continue
        corrected = min(c["t"] for c in legacy) - offset
        twin = standing.get((keep["count"], iso(corrected)))
        if twin is not None:
            drops.append((keep["id"], twin))
            stats["drop_late_twin"] += 1
            continue
        retime.append((keep["id"], corrected))
        stats["retime_legacy" if keep["padded"] else "retime_future"] += 1

    return {"drops": drops, "retime": retime, "stats": stats, "pairs": pairs}


def apply_source(conn: sqlite3.Connection, source_id: int, plan: dict) -> int:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS dup(id INTEGER PRIMARY KEY, keep INTEGER)")
    conn.execute("DELETE FROM dup")
    conn.executemany("INSERT INTO dup(id, keep) VALUES (?,?)", plan["drops"])
    moved = conn.execute(
        "INSERT OR IGNORE INTO receptions(packet_id, reporter_id, received_at) "
        "SELECT d.keep, r.reporter_id, r.received_at FROM dup d "
        "JOIN receptions r ON r.packet_id=d.id"
    ).rowcount
    # The kept copy was first heard whenever the earliest of its copies was.
    conn.execute(
        "UPDATE packets SET first_heard=(SELECT MIN(p.first_heard) FROM packets p "
        "  WHERE p.id=packets.id OR p.id IN (SELECT d.id FROM dup d WHERE d.keep=packets.id)) "
        "WHERE id IN (SELECT keep FROM dup)"
    )
    # Readings and receptions go with them by cascade.
    conn.execute("DELETE FROM packets WHERE id IN (SELECT id FROM dup)")

    for packet_id, t in plan["retime"]:
        data = json.loads(conn.execute(
            "SELECT data FROM packets WHERE id=?", (packet_id,)).fetchone()[0])
        data["time"] = wire_time(t)
        conn.execute(
            "UPDATE packets SET payload_time=?, time_epoch=?, data=?, "
            "altitude=NULL, altitude_resolved=0 WHERE id=?",
            (iso(t), t, json.dumps(data), packet_id))
        conn.execute("UPDATE readings SET ts=?, ts_epoch=? WHERE packet_id=?",
                     (iso(t), t, packet_id))

    conn.execute(
        "UPDATE sources SET packet_count=(SELECT COUNT(*) FROM packets WHERE source_id=:s), "
        "first_seen=(SELECT MIN(payload_time) FROM packets WHERE source_id=:s), "
        "last_seen=(SELECT MAX(payload_time) FROM packets WHERE source_id=:s) WHERE id=:s",
        {"s": source_id})
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.getenv("WENET_DB_PATH", "telemetry.db"),
                    help="database path (default: $WENET_DB_PATH or telemetry.db)")
    ap.add_argument("--flight", type=int, required=True, help="flight id")
    ap.add_argument("--source", help="limit to one source_key (default: every sensor source)")
    # Wide on purpose: one copy can arrive live and another from a station's
    # spool many minutes later. All it has to rule out is a reboot reusing a
    # count *and* repeating every reading to the float inside the window.
    ap.add_argument("--window", type=float, default=3600.0,
                    help="max seconds between copies' first reception (default 3600)")
    ap.add_argument("--apply", action="store_true", help="actually write")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Without this the cascades to readings and receptions silently do nothing.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")

    where, params = "flight_id=? AND kind='sensor'", [args.flight]
    if args.source:
        where += " AND source_key=?"
        params.append(args.source)
    sources = conn.execute(
        f"SELECT id, source_key, packet_count FROM sources WHERE {where} ORDER BY source_key",
        params).fetchall()
    if not sources:
        print(f"no sensor sources in flight {args.flight}"
              + (f" with key {args.source!r}" if args.source else ""))
        return 1

    print(f"database: {path}\nflight:   {args.flight}\n")
    plans = {}
    for s in sources:
        plan = plan_source(conn, s["id"], args.window)
        plans[s["id"]] = plan
        st, pairs = plan["stats"], plan["pairs"]
        print(f"{s['source_key']}: {s['packet_count']:,} packets -> "
              f"{s['packet_count'] - len(plan['drops']):,}")
        print(f"  drop   {st['drop_legacy']:,} legacy copies, {st['drop_twin']:,} duplicate "
              f"copies, {st['drop_future']:,} copies stamped in the future")
        if pairs:
            offs = sorted(p[1] for p in pairs)
            print(f"  legacy clock error from {len(pairs):,} pairs: median "
                  f"{statistics.median(offs):+.3f} s (middle 98%: {offs[len(offs) // 100]:+.3f} "
                  f"to {offs[-len(offs) // 100 - 1]:+.3f})")
        print(f"  retime {st['retime_legacy']:,} legacy-only samples, "
              f"{st['retime_future']:,} future-stamped samples")
        if st["drop_late_twin"]:
            print(f"  drop   {st['drop_late_twin']:,} legacy copies that re-time onto an "
                  "identical sample heard later")
        if st["unfixable"]:
            print(f"  LEAVE  {st['unfixable']:,} samples with no trustworthy copy or offset")

    total = sum(len(p["drops"]) for p in plans.values())
    retimed = sum(len(p["retime"]) for p in plans.values())
    if not args.apply:
        print(f"\ndry run -- {total:,} packets would go and {retimed:,} be re-timed. "
              "Re-run with --apply to write.")
        return 0

    with conn:   # one transaction; a failure part-way leaves the store untouched
        moved = sum(apply_source(conn, sid, plans[sid]) for sid in plans)
    conn.close()
    print(f"\ndeleted {total:,} packets, re-timed {retimed:,}; "
          f"moved {moved:,} receptions onto the copies kept.")

    # Re-timed packets were marked unresolved; let the store place them on the track.
    from app.store import Store
    store = Store(path)
    filled = store.backfill_altitudes(args.flight, flush=True)
    store.close()
    print(f"altitudes re-resolved: {filled['resolved']:,} ({filled['filled']:,} with a fix nearby)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
