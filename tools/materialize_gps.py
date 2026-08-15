#!/usr/bin/env python3
"""Make the GPS telemetry in an existing flight chartable.

A Wenet GPS packet carries far more than a fix: radio and CPU temperature, CPU
clock, load averages, disk, camera lens position and focus, battery volts and
amps, an auxiliary temperature. Filed only as a map point, all of that lands in
the `gps.extra` JSON blob -- stored, exported, and impossible to plot.

Newly ingested packets are now also filed as a source, so those values behave
like any other series. This backfills the same thing for flights already in the
store, reading the columns and the blob back into a decoded packet and writing
it through the identical path.

Idempotent: the packet insert is keyed on (source, time), so running it twice
adds nothing and a run interrupted half way finishes cleanly on the next.

    python tools/materialize_gps.py --list
    python tools/materialize_gps.py 1
    python tools/materialize_gps.py --all

Stop the server first, or run it against a copy. This writes a lot of rows in
one transaction, and the server holds source and field ids in memory for the
life of the process.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("flight", type=int, nargs="?", help="flight id to backfill")
    ap.add_argument("--all", action="store_true", help="every flight in the database")
    ap.add_argument("--list", action="store_true",
                    help="show what each flight would gain, and write nothing")
    ap.add_argument("--db", default=os.getenv("WENET_DB_PATH", "telemetry.db"),
                    help="database path (default: $WENET_DB_PATH or telemetry.db)")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no such database: {path}", file=sys.stderr)
        return 1
    if not (args.all or args.list or args.flight is not None):
        ap.error("give a flight id, or --all, or --list")

    store = Store(path)
    try:
        conn = store.pool.get()
        flights = conn.execute(
            "SELECT f.id, f.name, "
            " (SELECT COUNT(*) FROM gps g WHERE g.flight_id=f.id) AS gps_rows, "
            " (SELECT COUNT(*) FROM sources s WHERE s.flight_id=f.id AND s.kind='gps') "
            "   AS has_source "
            "FROM flights f ORDER BY f.id"
        ).fetchall()

        if args.list:
            print(f"database: {path}")
            for row in flights:
                state = "done" if row["has_source"] else "pending"
                print(f"  flight {row['id']:>3}  {row['name']:<24} "
                      f"{row['gps_rows']:>7,} gps rows  {state}")
            return 0

        targets = [r["id"] for r in flights] if args.all else [args.flight]
        known = {r["id"] for r in flights}
        for flight_id in targets:
            if flight_id not in known:
                print(f"no flight {flight_id}", file=sys.stderr)
                return 1

        for flight_id in targets:
            out = store.materialize_gps_telemetry(flight_id)
            fields = ", ".join(out["fields"]) or "none"
            print(f"flight {flight_id}: {out['gps_rows']:,} gps rows, "
                  f"{out['packets_created']:,} packets written")
            print(f"  new fields: {fields}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
