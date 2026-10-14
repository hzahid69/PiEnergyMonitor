"""Read live Shelly power from InfluxDB and send Device Left-On alerts."""

import csv
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from device_left_on import DeviceChecker, read_time
from settings import (
    POWER_LIMIT, TIME_LIMIT, MAX_GAP, INFLUX_URL, ORG, BUCKET,
    POLL_SECONDS, GRAFANA_URL, GRAFANA_DASHBOARD_UID,
)


def read_influx(token):
    """Read recent apower measurements; do not modify InfluxDB."""
    minutes = max(20, TIME_LIMIT + 5)
    query = (
        f'from(bucket: {json.dumps(BUCKET)})\n'
        f'  |> range(start: -{minutes}m)\n'
        '  |> filter(fn: (r) => r._measurement == "shelly_plug" '
        'and r._field == "apower")\n'
        '  |> keep(columns: ["_time", "_value", "_field"])\n'
        '  |> sort(columns: ["_time"])'
    )
    address = INFLUX_URL.rstrip("/") + "/api/v2/query?" + urlencode({"org": ORG})
    request = Request(
        address,
        data=query.encode("utf-8"),
        headers={
            "Authorization": "Token " + token,
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        method="POST",
    )
    with urlopen(request, timeout=15) as result:
        return parse_influx_csv(result.read().decode("utf-8-sig"))


def parse_influx_csv(csv_text):
    """Turn InfluxDB's annotated CSV into sorted (time, watts) pairs."""
    readings = {}
    header = None

    for cells in csv.reader(io.StringIO(csv_text)):
        if not cells or cells[0].startswith("#"):
            continue
        if "_time" in cells and "_value" in cells:
            header = cells
            continue
        if header is None or len(cells) != len(header):
            continue

        row = dict(zip(header, cells))
        if row.get("_field") != "apower":
            continue
        try:
            timestamp = read_time(row["_time"])
            watts = float(row["_value"])
            if timestamp.tzinfo is None or not math.isfinite(watts) or watts < 0:
                continue
        except (ValueError, KeyError):
            continue
        readings[timestamp] = watts

    return sorted(readings.items())


def add_grafana_marker(time, power, token):
    """Add a marker on the Grafana graph when an alert happens."""
    message = (f"Device Left-On Alert: {power:g} W. "
               f"Power >= {POWER_LIMIT:g} W for {TIME_LIMIT:g} minutes.")
    data = {
        "time": int(time.timestamp() * 1000),
        "text": message,
        "tags": ["group13", "device-left-on"],
    }
    if GRAFANA_DASHBOARD_UID:
        data["dashboardUID"] = GRAFANA_DASHBOARD_UID

    request = Request(
        GRAFANA_URL.rstrip("/") + "/api/annotations",
        data=json.dumps(data).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as result:
        result.read()


def save_alert(time, power):
    """Keep a simple local record of alerts."""
    import os.path
    filename = "alerts.csv"
    new_file = not os.path.exists(filename) or os.path.getsize(filename) == 0
    with open(filename, "a", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        if new_file:
            writer.writerow(["timestamp", "power_w", "message"])
        writer.writerow([time.isoformat(), power, "Device may have been left ON"])


def run(once=False):
    token = os.environ.get("INFLUX_TOKEN", "")
    grafana_token = os.environ.get("GRAFANA_TOKEN", "")
    if POWER_LIMIT <= 0 or TIME_LIMIT <= 0 or MAX_GAP <= 0 or POLL_SECONDS <= 0:
        print("ERROR: Power, time, gap and poll settings must be positive.")
        return 2
    if not token:
        print("ERROR: Set INFLUX_TOKEN (read-only) before starting.")
        return 2
    if ORG == "CHANGE_ME" or BUCKET == "CHANGE_ME":
        print("ERROR: Set ORG and BUCKET in settings.py first.")
        return 2

    checker = DeviceChecker()
    started_at = datetime.now(timezone.utc)
    print("Reading Shelly power from InfluxDB. Press Ctrl+C to stop.", flush=True)
    if not grafana_token and not once:
        print("Grafana markers disabled (GRAFANA_TOKEN not set).", flush=True)

    while True:
        try:
            readings = read_influx(token)
            if once:
                if readings:
                    last_time, last_power = readings[-1]
                    print(f"Connection OK. {len(readings)} readings; latest {last_power:g} W at {last_time}.")
                else:
                    print("Connection OK, but no apower readings were found.")
                return 0

            new_count = 0
            for timestamp, power in readings:
                if checker.last_time is not None and timestamp <= checker.last_time:
                    continue
                new_count += 1
                # Alert only on a fresh sample, not on old stored history.
                fresh = (datetime.now(timezone.utc) - timestamp).total_seconds() <= MAX_GAP
                if checker.check(timestamp, power, allow_alert=timestamp >= started_at and fresh):
                    print("ALERT: Device may have been left ON!", flush=True)
                    print("Time:", timestamp, "Power:", power, "W", flush=True)
                    save_alert(timestamp, power)
                    if grafana_token:
                        try:
                            add_grafana_marker(timestamp, power, grafana_token)
                            print("Grafana marker created.", flush=True)
                        except (HTTPError, URLError, OSError) as error:
                            print("Grafana marker failed:", type(error).__name__,
                                  getattr(error, "code", ""), flush=True)

            if readings:
                last_time, last_power = readings[-1]
                age = (datetime.now(timezone.utc) - last_time).total_seconds()
                if age > MAX_GAP:
                    print("Warning: Shelly data is old; check data collection.", flush=True)
                else:
                    print(f"New readings: {new_count}, latest: {last_power:g} W", flush=True)
            else:
                print("No apower readings found in InfluxDB.", flush=True)

            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("Stopped.")
            return 0
        except HTTPError as error:
            print("InfluxDB HTTP", error.code, "- check token, org and bucket.", flush=True)
            if once:
                return 2
            time.sleep(POLL_SECONDS)
        except (URLError, OSError, ValueError) as error:
            print("Cannot read InfluxDB:", type(error).__name__, flush=True)
            if once:
                return 2
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--once"):
        print("Usage: python3 live_monitor.py [--once]")
        sys.exit(2)
    sys.exit(run(once="--once" in sys.argv))
