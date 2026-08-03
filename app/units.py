"""Seed metadata for discovered fields.

Nothing here is authoritative and nothing here gates ingest -- a field the
server has never heard of is stored exactly the same way as one it knows, it
just renders without a unit until somebody labels it. This table only exists so
that the common cases arrive pre-labelled.

The bulk of it is ported from the UNITS map in Wenet_BLE_Sensor/configurator.html
so the web display and the payload configurator agree on terminology.
"""

from __future__ import annotations

# name -> (unit, description)
UNITS: dict[str, tuple[str, str]] = {
    # --- envelope ---------------------------------------------------------
    "time": ("", "Timestamp from the payload RTC"),
    "id": ("", "payload_name + task name"),
    "count": ("", "Packet counter, wraps at 65536"),
    "payload": ("", "Human-readable payload name"),
    # --- housekeeping -----------------------------------------------------
    "v_in": ("V", "VSYS via the internal divider (get_batt)"),
    "vbatt": ("V", "Scaled battery voltage"),
    "pi_temp": ("°C", "RP2040 core temperature"),
    "adc0": ("counts", "12-bit, J5 on the HAT (direct 0-3.3 V)"),
    "adc1": ("counts", "12-bit, J4 on the HAT (direct 0-3.3 V)"),
    "adc2": ("counts", "12-bit, J2 on the HAT (divided only if JP3 is fitted)"),
    # --- Bosch ------------------------------------------------------------
    "bmp_temp": ("°C", "BMP280"),
    "bmp_pres": ("hPa", "BMP280"),
    "bme_temp": ("°C", "BME680"),
    "bme_pres": ("hPa", "BME680"),
    "bme_humi": ("%RH", "BME680"),
    "bme280_temp": ("°C", "BME280"),
    "bme280_pres": ("hPa", "BME280"),
    "bme280_humi": ("%RH", "BME280"),
    # --- other environmental ---------------------------------------------
    "hdc_temp": ("°C", "HDC302x"),
    "hdc_humi": ("%RH", "HDC302x"),
    "max31725_temp": ("°C", "MAX31725"),
    "hsc_temp": ("°C", "Honeywell HSC"),
    "hsc_pres": ("", "Honeywell HSC"),
    # --- magnetometer / IMU ----------------------------------------------
    "mag_x": ("µT", "LIS3MDL"),
    "mag_y": ("µT", "LIS3MDL"),
    "mag_z": ("µT", "LIS3MDL"),
    "a_x": ("m/s²", "LSM6DSOX accelerometer"),
    "a_y": ("m/s²", "LSM6DSOX accelerometer"),
    "a_z": ("m/s²", "LSM6DSOX accelerometer"),
    "g_x": ("°/s", "LSM6DSOX gyroscope"),
    "g_y": ("°/s", "LSM6DSOX gyroscope"),
    "g_z": ("°/s", "LSM6DSOX gyroscope"),
    "imu_temp": ("°C", "LSM6DSOX"),
    "fall_cnt": ("", "LSM6DSOX free-fall events"),
    # --- generic names used by earlier firmware (seen in the RAB-8 logs) ---
    # These are unqualified because that firmware did not namespace by sensor.
    "temp": ("°C", "Temperature"),
    "pres": ("hPa", "Pressure"),
    "humi": ("%RH", "Relative humidity"),
    # Unit deliberately left blank: the HSC differential part is configured per
    # build and guessing Pa vs psi here would silently mislabel a real series.
    "diff_pressure": ("", "Differential pressure"),
    # --- GPS (wenet GPS_TELEMETRY, packet type 0x01) ----------------------
    "latitude": ("°", "GPS latitude"),
    "longitude": ("°", "GPS longitude"),
    "altitude": ("m", "GPS altitude above ellipsoid"),
    "ascent_rate": ("m/s", "Vertical rate"),
    "ground_speed": ("m/s", "Horizontal speed"),
    "heading": ("°", "Course over ground"),
    "numSV": ("", "Satellites used in solution"),
    "batt_v": ("V", "Wenet payload battery"),
    "batt_i": ("mA", "Wenet payload current"),
    "cpu_temp": ("°C", "Wenet payload CPU"),
    "radio_temp": ("°C", "Wenet payload radio"),
    "sensor_temp": ("°C", "Wenet camera sensor"),
    "aux_temp": ("°C", "Wenet auxiliary sensor"),
}

#: Fields that describe the packet rather than measure anything. They are kept
#: in the packet archive but are not registered as chartable series.
RESERVED = frozenset({"time", "id", "count", "reporter_id"})


def field_meta(name: str) -> tuple[str, str]:
    """Return ``(unit, description)`` for a field name, best effort."""
    if name in UNITS:
        return UNITS[name]
    # DS18X20 sensors name themselves at runtime from the last two ROM bytes,
    # so they can never appear in a static table -- see main.py:341-346.
    if name.startswith("temp-"):
        return ("°C", f"OneWire DS18X20 (ROM ...{name[5:]})")
    return ("", "")
