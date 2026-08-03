"""Tests for the capture-format parsers in tools/replay.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.replay import parse_line  # noqa: E402


def test_self_describing_csv():
    """The format wenet_ble_client writes: time, count, id, then key/value pairs."""
    line = "2025-11-01 13:12:20.025,412,RAB_HAT_ENV,v_in,4.021,bmp_temp,-54.200"
    record = parse_line(line, None)

    assert record == {
        "time": "2025-11-01 13:12:20.025",
        "count": 412,
        "id": "RAB_HAT_ENV",
        "v_in": 4.021,
        "bmp_temp": -54.2,
    }


def test_real_capture_lines():
    """Verbatim lines from a real sensor_data_*.log off the balloon Pi."""
    env = parse_line(
        "2026-07-26 20:55:39.020,22561,GDB_ENV_ENV,adc2,3,adc1,4,v_in,6.126,"
        "hdc_temp,27.267,adc0,3,pi_temp,31,hdc_humi,41.865,max31725_temp,27.617",
        None,
    )
    assert env["id"] == "GDB_ENV_ENV"
    assert env["count"] == 22561
    assert env["hdc_temp"] == 27.267
    assert env["max31725_temp"] == 27.617
    assert env["pi_temp"] == 31 and isinstance(env["pi_temp"], int)

    imu = parse_line(
        "2026-07-26 20:55:39.055,22561,GDB_ENV_LSM6DSOX,a_z,0.278,a_y,0.006,"
        "a_x,-0.960,g_y,4.700,g_x,0.000,imu_temp,26.613,fall_cnt,0,g_z,-0.854",
        None,
    )
    # Same count as the ENV packet above, from a different task on the same
    # payload -- the case that makes `id` rather than `count` the identity.
    assert imu["id"] == "GDB_ENV_LSM6DSOX"
    assert imu["count"] == 22561
    assert imu["a_x"] == -0.960
    assert imu["fall_cnt"] == 0


def test_positional_sd_card_csv():
    """SD-card CSV: time, id, count, then the universal prefix positionally."""
    line = "2025-11-01 13:12:20.025,RAB_HAT_ENV,412,4.021,23,1744,12,2043"
    record = parse_line(line, None)

    assert record["id"] == "RAB_HAT_ENV"
    assert record["count"] == 412
    assert record["v_in"] == 4.021
    assert record["adc2"] == 2043


def test_positional_csv_names_unknown_trailing_columns():
    line = "2025-11-01 13:12:20.025,RAB_HAT_ENV,412,4.0,23,1,2,3,-54.2,72.4"
    record = parse_line(line, None)

    assert record["extra0"] == -54.2
    assert record["extra1"] == 72.4


def test_positional_csv_with_explicit_columns():
    line = "2025-11-01 13:12:20.025,RAB_HAT_ENV,412,4.0,23,1,2,3,-54.2"
    columns = ["v_in", "pi_temp", "adc0", "adc1", "adc2", "bmp_temp"]
    record = parse_line(line, columns)

    assert record["bmp_temp"] == -54.2


def test_json_lines():
    line = '{"time": "20251101T132850.002Z", "id": "generic", "count": 2, "pres": 994.8}'
    record = parse_line(line, None)

    assert record["id"] == "generic"
    assert record["pres"] == 994.8


def test_blank_and_malformed_lines_are_skipped():
    assert parse_line("", None) is None
    assert parse_line("   ", None) is None
    assert parse_line("not,a", None) is None
    assert parse_line("{bad json", None) is None
