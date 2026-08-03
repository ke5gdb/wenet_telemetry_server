#!/usr/bin/env python3
"""Replay a captured telemetry log into the ingest endpoint.

This exists so that every later part of the system -- charts, the map, SSE,
exports -- can be built and tested at a desk instead of requiring a balloon.

Three capture formats are recognised, sniffed per line:

1. **Self-describing CSV** -- ``sensor_data_*.log``, written by
   ``_log_sensor_data`` in wenet_ble_scripts/wenet_ble_client.py::

       2025-11-01 13:12:20.025,412,RAB_HAT_ENV,v_in,4.021,bmp_temp,-54.200

   ``time, count, id`` then inline ``key,value`` pairs. Preferred: it carries
   its own field names, so it survives any firmware change.

2. **JSON Lines** -- one decoded packet per line, as in the RAB-8 analysis logs.

3. **Positional CSV** -- ``data_log_*.csv`` from the payload's SD card. No
   header row and the trailing columns depend on which sensors were detected at
   boot, so anything past the universal prefix is named ``extraN`` unless
   ``--columns`` is given.

Multiple ground stations can be simulated with ``--reporters``, which is how the
deduplication path gets exercised: the same packet arrives several times under
different callsigns and must collapse to one sample with several receptions.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.timeutil import iso, parse_ts  # noqa: E402

#: Universal leading columns of the SD-card CSV, from main.py:229.
POSITIONAL_PREFIX = ["v_in", "pi_temp", "adc0", "adc1", "adc2"]


# -- parsing --------------------------------------------------------------


def coerce(value: str):
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_line(line: str, columns: list[str] | None) -> dict | None:
    line = line.strip()
    if not line:
        return None

    if line.startswith("{"):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None

    # Format 1 has count second and id third; format 3 has them the other way
    # round. That is enough to tell them apart without guessing.
    if isinstance(coerce(parts[1]), int):
        return _parse_self_describing(parts)
    if isinstance(coerce(parts[2]), int):
        return _parse_positional(parts, columns)
    return None


def _parse_self_describing(parts: list[str]) -> dict | None:
    record = {"time": parts[0], "count": coerce(parts[1]), "id": parts[2]}
    rest = parts[3:]
    for i in range(0, len(rest) - 1, 2):
        name = rest[i]
        if name:
            record[name] = coerce(rest[i + 1])
    return record


def _parse_positional(parts: list[str], columns: list[str] | None) -> dict:
    record = {"time": parts[0], "id": parts[1], "count": coerce(parts[2])}
    names = columns or POSITIONAL_PREFIX
    for i, value in enumerate(parts[3:]):
        if value == "":
            continue
        name = names[i] if i < len(names) else f"extra{i - len(names)}"
        record[name] = coerce(value)
    return record


def read_records(path: Path, columns: list[str] | None, limit: int | None):
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            record = parse_line(line, columns)
            if record is None or "time" not in record or "id" not in record:
                continue
            yield record
            count += 1
            if limit and count >= limit:
                return


# -- synthetic GPS --------------------------------------------------------


def synth_gps(start, end, args) -> list[dict]:
    """A plausible ascent/burst/descent profile spanning the replayed data.

    Only for bench testing. It exists so the altitude axis and the map have
    something to draw before a real GPS capture is available.
    """
    total = (end - start).total_seconds()
    if total <= 0:
        return []

    # Align to whole seconds, as a real receiver's GPS timestamps are. Leaving
    # them on the first sensor sample's sub-second offset made every altitude
    # join miss, which is misleading on the bench in a way real data would not be.
    start = start.replace(microsecond=0)

    climb_s = (args.burst_alt - args.ground_alt) / args.ascent_rate
    points = []
    lat, lon = args.launch_lat, args.launch_lon

    step = args.gps_interval
    t = 0.0
    while t <= total:
        if t <= climb_s:
            alt = args.ground_alt + args.ascent_rate * t
            rate = args.ascent_rate
        else:
            # Descent under parachute: fast up high, slowing as air thickens.
            fall = t - climb_s
            alt = max(args.ground_alt, args.burst_alt - args.descent_rate * fall)
            rate = -args.descent_rate if alt > args.ground_alt else 0.0

        # Steady drift, so the track is a line rather than a dot.
        lat += args.drift_lat * step
        lon += args.drift_lon * step

        points.append(
            {
                "kind": "gps",
                "data": {
                    "timestamp": iso(start + timedelta(seconds=t)),
                    "latitude": round(lat, 6),
                    "longitude": round(lon, 6),
                    "altitude": round(alt, 1),
                    "ascent_rate": round(rate, 2),
                    "ground_speed": 12.0,
                    "heading": 84.0,
                    "numSV": 9,
                    "gpsFix": 3,
                },
            }
        )
        t += step

    return points


# -- sending --------------------------------------------------------------


class Sender:
    def __init__(self, args):
        self.args = args
        self.client = None if args.dry_run else httpx.Client(timeout=30.0)
        self.totals = {"accepted": 0, "duplicates": 0, "rejected": 0,
                       "gps_accepted": 0, "gps_duplicates": 0}
        self.sent = 0
        self.buffers: dict[str, list[dict]] = {}

    def add(self, reporter: str, packet: dict) -> None:
        buf = self.buffers.setdefault(reporter, [])
        buf.append(packet)
        if len(buf) >= self.args.batch:
            self.flush(reporter)

    def flush(self, reporter: str | None = None) -> None:
        targets = [reporter] if reporter else list(self.buffers)
        for name in targets:
            buf = self.buffers.get(name)
            if not buf:
                continue
            self._post(name, buf)
            self.buffers[name] = []

    def _post(self, reporter: str, packets: list[dict]) -> None:
        self.sent += len(packets)
        if self.client is None:
            return
        body = {"reporter_id": reporter, "packets": packets}
        headers = {"X-API-KEY": self.args.api_key} if self.args.api_key else {}
        for attempt in range(6):
            try:
                resp = self.client.post(self.args.url, json=body, headers=headers)
                if resp.status_code < 300:
                    for key in self.totals:
                        self.totals[key] += resp.json().get(key, 0)
                    return
                # 429 and 5xx are transient: back off and retry, never drop.
                # Anything else is a client error that a retry cannot fix.
                if resp.status_code != 429 and resp.status_code < 500:
                    print(f"  ! {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                    return
                if attempt == 0:
                    print(f"  ! {resp.status_code}, backing off", file=sys.stderr)
            except httpx.HTTPError as exc:
                print(f"  ! {exc}", file=sys.stderr)
            time.sleep(min(0.25 * 2**attempt, 8.0))
        print(f"  ! giving up on {len(packets)} packets", file=sys.stderr)

    def close(self) -> None:
        self.flush()
        if self.client is not None:
            self.client.close()


# -- main -----------------------------------------------------------------


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)
    path = Path(args.logfile)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    columns = args.columns.split(",") if args.columns else None
    reporters = [r.strip() for r in args.reporters.split(",") if r.strip()]

    records = list(read_records(path, columns, args.limit))
    if not records:
        print("no parseable records found", file=sys.stderr)
        return 1

    times = []
    for record in records:
        try:
            times.append(parse_ts(record["time"]))
        except ValueError:
            times.append(None)
    known = [t for t in times if t]
    if not known:
        print("no parseable timestamps", file=sys.stderr)
        return 1

    span = (max(known) - min(known)).total_seconds()
    sources = sorted({r.get("id", "?") for r in records})
    print(f"{len(records)} records | {len(sources)} sources | {span / 60:.1f} min of data")
    print(f"sources: {', '.join(sources)}")
    print(f"reporters: {', '.join(reporters)}  hear-rate {args.hear_rate}")
    if args.dry_run:
        print("dry run -- nothing will be sent")
    print()

    sender = Sender(args)
    gps_queue = synth_gps(min(known), max(known), args) if args.synth_gps else []
    gps_index = 0
    started = time.monotonic()
    previous = None

    try:
        for record, stamp in zip(records, times):
            if args.speed > 0 and previous is not None and stamp is not None:
                delta = (stamp - previous).total_seconds()
                if 0 < delta < 3600:
                    time.sleep(delta / args.speed)
            if stamp is not None:
                previous = stamp

            # Release any synthetic GPS that is now due.
            while gps_index < len(gps_queue) and stamp is not None:
                due = parse_ts(gps_queue[gps_index]["data"]["timestamp"])
                if due > stamp:
                    break
                for reporter in reporters:
                    sender.add(reporter, dict(gps_queue[gps_index]))
                gps_index += 1

            packet = {"kind": "sensor", "data": record}
            heard = [r for r in reporters if rng.random() <= args.hear_rate]
            for reporter in heard:
                sender.add(reporter, dict(packet))

            if sender.sent and sender.sent % args.progress == 0:
                elapsed = time.monotonic() - started
                print(f"  {sender.sent:>7} sent  {sender.sent / max(elapsed, 1e-6):7.0f}/s")
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
    finally:
        sender.close()

    elapsed = time.monotonic() - started
    print(f"\nsent {sender.sent} packets in {elapsed:.1f}s")
    if not args.dry_run:
        t = sender.totals
        print(f"  accepted   {t['accepted']}")
        print(f"  duplicates {t['duplicates']}   <- receive diversity, collapsed to one sample")
        print(f"  rejected   {t['rejected']}")
        if args.synth_gps:
            print(f"  gps        {t['gps_accepted']} accepted, {t['gps_duplicates']} duplicate")
    return 0


def parse_args():
    p = argparse.ArgumentParser(
        description="Replay a captured telemetry log into the ingest endpoint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  # as fast as possible, one station\n"
            "  python tools/replay.py sensor_data_2026-07-26.log --speed 0\n\n"
            "  # real time, three stations with patchy copy, plus synthetic GPS\n"
            "  python tools/replay.py flight.log --reporters KE5GDB,K5NBC,W5ABC \\\n"
            "      --hear-rate 0.8 --synth-gps\n\n"
            "  # just check the file parses\n"
            "  python tools/replay.py flight.log --dry-run --limit 20\n"
        ),
    )
    p.add_argument("logfile")
    p.add_argument("--url", default="http://127.0.0.1:8000/api/v1/ingest")
    p.add_argument("--api-key", default=None)
    p.add_argument("--reporters", default="REPLAY",
                   help="comma-separated callsigns to simulate (default: REPLAY)")
    p.add_argument("--hear-rate", type=float, default=1.0,
                   help="probability each reporter hears a given packet (default: 1.0)")
    p.add_argument("--speed", type=float, default=0.0,
                   help="playback speed multiplier; 0 = no pacing (default: 0)")
    p.add_argument("--batch", type=int, default=15,
                   help="packets per POST, matching the forwarder (default: 15)")
    p.add_argument("--limit", type=int, default=None, help="stop after N records")
    p.add_argument("--columns", default=None,
                   help="comma-separated column names for positional SD-card CSV")
    p.add_argument("--dry-run", action="store_true", help="parse only, send nothing")
    p.add_argument("--seed", type=int, default=1, help="RNG seed for hear-rate")
    p.add_argument("--progress", type=int, default=500)

    g = p.add_argument_group("synthetic GPS (bench testing only)")
    g.add_argument("--synth-gps", action="store_true")
    g.add_argument("--gps-interval", type=float, default=1.0)
    g.add_argument("--launch-lat", type=float, default=32.9)
    g.add_argument("--launch-lon", type=float, default=-97.2)
    g.add_argument("--ground-alt", type=float, default=200.0)
    g.add_argument("--burst-alt", type=float, default=30000.0)
    g.add_argument("--ascent-rate", type=float, default=5.0)
    g.add_argument("--descent-rate", type=float, default=8.0)
    g.add_argument("--drift-lat", type=float, default=0.00002)
    g.add_argument("--drift-lon", type=float, default=0.00006)
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
