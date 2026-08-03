#!/usr/bin/env python3
"""Emit Wenet UDP frames as the receiver would, to exercise wenet_forwarder.

`replay.py` tests the server by posting to its HTTP endpoint directly. This sits
one stage earlier and tests the *forwarder*: it broadcasts the same UDP JSON
frames that `rx_ssdv.py` produces, so the whole ground-station chain -- packet
type dispatch, CBOR decode, GPS decode, batching, spooling -- can be run on a
bench with no SDR and no balloon.

    python tools/fake_receiver.py --port 55672 --duration 30

Sensor packets are type 0x03 carrying CBOR; GPS is type 0x01 carrying the
81-byte binary struct. Both are padded with 0x55 exactly as the modem does.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import random
import socket
import struct
import time

import cbor2

GPS_STRUCT = ">BHIBffffffBBBffHfffffffHHf"
GPS_EPOCH = datetime.datetime(1980, 1, 6, 0, 0, 0)
LEAP_SECONDS = 18
SEC_PAYLOAD_ID = 55
FRAME_LEN = 256
PAD = 0x55


def gps_week_seconds(when: datetime.datetime) -> tuple[int, int]:
    total = (when - GPS_EPOCH).total_seconds() + LEAP_SECONDS
    week = int(total // (7 * 86400))
    return week, int((total - week * 7 * 86400) * 1000)


def build_gps_packet(when, lat, lon, alt, ascent, fix=3, sats=9) -> bytes:
    week, itow_ms = gps_week_seconds(when)
    packet = struct.pack(
        GPS_STRUCT,
        0x01,           # packet type
        week, itow_ms, LEAP_SECONDS,
        lat, lon, alt,
        12.0,           # ground speed
        84.0,           # heading
        ascent,
        sats, fix,
        6,              # dynamic model (airborne <1g)
        31.5, 44.2,     # radio_temp, cpu_temp
        1500,           # cpu_speed -- not 0x5555, so extended fields decode
        0.4, 0.35, 0.3, # load averages
        42.0,           # disk percent
        9.1,            # lens position
        28.3,           # sensor temp
        1234,           # focus fom
        7912,           # batt_v in mV -- not 0x5555, so battery fields decode
        450,            # batt_i
        19.5,           # aux temp
    )
    return packet.ljust(FRAME_LEN, bytes([PAD]))


def build_sensor_packet(payload: dict) -> bytes:
    body = cbor2.dumps(payload)
    frame = bytes([0x03, SEC_PAYLOAD_ID]) + body
    if len(frame) > FRAME_LEN:
        raise ValueError(f"sensor packet too long: {len(frame)} bytes")
    return frame.ljust(FRAME_LEN, bytes([PAD]))


def send(sock, host, port, packet: bytes) -> None:
    frame = {"type": "WENET", "packet": list(bytearray(packet))}
    sock.sendto(json.dumps(frame).encode("ascii"), (host, port))


def main() -> int:
    args = parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    started = time.monotonic()
    counts = {"gps": 0, "sensor": 0, "nofix": 0}
    count = 0
    lat, lon = args.lat, args.lon

    print(f"emitting to {args.host}:{args.port} for {args.duration}s "
          f"(sensors {args.rate} Hz, gps 1 Hz)")

    while time.monotonic() - started < args.duration:
        elapsed = time.monotonic() - started
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

        # A simple ascent so altitude actually moves during the run.
        alt = args.ground_alt + args.ascent_rate * elapsed
        lat += 0.00002
        lon += 0.00006

        if count % max(int(args.rate), 1) == 0:
            # The first few seconds have no fix, as a real payload does at
            # power-on -- the forwarder must drop those, not send 0,0.
            if elapsed < args.nofix_seconds:
                send(sock, args.host, args.port,
                     build_gps_packet(now, 0.0, 0.0, 0.0, 0.0, fix=0, sats=0))
                counts["nofix"] += 1
            else:
                send(sock, args.host, args.port,
                     build_gps_packet(now, lat, lon, alt, args.ascent_rate))
                counts["gps"] += 1

        stamp = now.strftime("%Y%m%dT%H%M%S.") + f"{now.microsecond // 1000:03d}Z"
        # Pressure falling roughly exponentially with height, as it does.
        pressure = 1013.25 * math.exp(-alt / 8400.0)
        send(sock, args.host, args.port, build_sensor_packet({
            "time": stamp,
            "id": args.source,
            "count": count,
            "v_in": round(4.05 - elapsed * 0.001, 3),
            "pi_temp": 23,
            "bmp_temp": round(15.0 - alt * 0.0065 + random.uniform(-0.2, 0.2), 2),
            "bmp_pres": round(pressure, 2),
        }))
        counts["sensor"] += 1

        count += 1
        time.sleep(1.0 / args.rate)

    print(f"sent {counts['sensor']} sensor, {counts['gps']} gps, "
          f"{counts['nofix']} no-fix gps packets")
    return 0


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=55672)
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--rate", type=float, default=2.0, help="sensor packets/sec")
    p.add_argument("--source", default="RAB_HAT_ENV")
    p.add_argument("--lat", type=float, default=32.9)
    p.add_argument("--lon", type=float, default=-97.2)
    p.add_argument("--ground-alt", type=float, default=200.0)
    p.add_argument("--ascent-rate", type=float, default=5.0)
    p.add_argument("--nofix-seconds", type=float, default=3.0,
                   help="emit no-fix GPS for this long at the start")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
