"""Read live Shelly power from InfluxDB and send Device Left-On alerts."""

import csv
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from device_left_on import DeviceChecker, read_time
from settings import (
    POWER_LIMIT, TIME_LIMIT, MAX_GAP, INFLUX_URL, ORG, BUCKET,
    POLL_SECONDS, GRAFANA_URL, GRAFANA_DASHBOARD_UID,
)


# The RGB alarm stays red until Shelly reports the device is OFF.
# Use a lower OFF limit so a small power dip does not cancel the alarm.
OFF_POWER_LIMIT = 1.0       # watts
OFF_CONFIRM_READINGS = 2   # consecutive fresh measurements
FUTURE_SKEW_SECONDS = 5   # accept a small clock difference
STATE_FILE = Path(__file__).with_name(".rgb_alert_state")


def get_rgb_panel():
    """Get the 8x8 Sense HAT LED panel if it is available."""
    try:
        from sense_hat import SenseHat
        return SenseHat()
    except (ImportError, OSError, RuntimeError) as error:
        print("Sense HAT not available:", type(error).__name__, flush=True)
        return None


def show_rgb(panel, red):
    """Turn the panel red, or switch it off."""
    if panel is None:
        return False
    try:
        if red:
            panel.clear(255, 0, 0)
        else:
            panel.clear()
        return True
    except (OSError, RuntimeError) as error:
        print("Sense HAT error:", type(error).__name__, flush=True)
        return False


class RgbAlarm:
    """Remember the alert and keep the Sense HAT in sync with it."""

    def __init__(self, panel, state_file=None):
        self.panel = panel
        self.state_file = Path(state_file) if state_file is not None else STATE_FILE
        self.active = self._load_state()
        self.off_readings = 0
        self.needs_sync = True
        self.sync_panel()

    def _load_state(self):
        try:
            saved = self.state_file.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return False
        except OSError:
            print("Warning: cannot read RGB state; keeping the warning ON.", flush=True)
            return True
        if saved not in ("0", "1"):
            print("Warning: unknown RGB state; keeping the warning ON.", flush=True)
            return True
        return saved == "1"

    def _save_state(self):
        """Save only 0 or 1. No tokens or power measurements are stored."""
        temp_file = self.state_file.with_name(self.state_file.name + ".tmp")
        try:
            temp_file.write_text("1" if self.active else "0", encoding="ascii")
            temp_file.replace(self.state_file)
        except OSError as error:
            print("Warning: cannot save RGB state:", type(error).__name__, flush=True)

    def sync_panel(self):
        """Keep red lit, and retry a failed switch-off on the next poll."""
        if self.active or self.needs_sync:
            self.needs_sync = not show_rgb(self.panel, self.active)
        return not self.needs_sync

    def start_alert(self):
        self.active = True
        self.off_readings = 0
        self._save_state()
        self.needs_sync = True
        return self.sync_panel()

    def check_off(self, power, fresh, continuous, after_start):
        """Clear only after two NEW, fresh, consecutive <=1 W readings."""
        if not self.active:
            return False
        if not (fresh and continuous and after_start):
            self.off_readings = 0
        elif power <= OFF_POWER_LIMIT:
            self.off_readings += 1
            if self.off_readings >= OFF_CONFIRM_READINGS:
                self.active = False
                self.off_readings = 0
                self._save_state()
                self.needs_sync = True
                self.sync_panel()
                return True
        else:
            self.off_readings = 0
        return False


def is_fresh_sample(timestamp):
    """Reject stale readings and timestamps far in the future."""
    age = (datetime.now(timezone.utc) - timestamp).total_seconds()
    return -FUTURE_SKEW_SECONDS <= age <= MAX_GAP


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
    rgb_alarm = RgbAlarm(get_rgb_panel()) if not once else None
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
                # Cached samples can warm up DeviceChecker, but must not clear RGB.
                fresh = is_fresh_sample(timestamp)
                gap = ((timestamp - checker.last_time).total_seconds()
                       if checker.last_time is not None else 0)
                continuous = checker.last_time is None or 0 < gap <= MAX_GAP
                if rgb_alarm.check_off(power, fresh, continuous, timestamp >= started_at):
                    if rgb_alarm.needs_sync:
                        print("Device OFF confirmed; RGB switch-off will be retried.", flush=True)
                    else:
                        print("Device OFF: RGB warning cleared.", flush=True)

                # DeviceChecker still handles the 8 W / 10 min detection.
                new_alert = checker.check(
                    timestamp, power, allow_alert=timestamp >= started_at and fresh
                )
                if new_alert and not rgb_alarm.active:
                    rgb_ok = rgb_alarm.start_alert()
                    print("ALERT: Device may have been left ON!", flush=True)
                    if rgb_ok:
                        print("RGB panel: RED until device is OFF.", flush=True)
                    else:
                        print("RGB panel unavailable; Terminal alert works.", flush=True)
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

            # Reassert red even when no new readings arrive. Never clear on data loss.
            rgb_alarm.sync_panel()
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("Stopped.")
            return 0
        except HTTPError as error:
            print("InfluxDB HTTP", error.code, "- check token, org and bucket.", flush=True)
            if once:
                return 2
            rgb_alarm.sync_panel()
            time.sleep(POLL_SECONDS)
        except (URLError, OSError, ValueError) as error:
            print("Cannot read InfluxDB:", type(error).__name__, flush=True)
            if once:
                return 2
            rgb_alarm.sync_panel()
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--once"):
        print("Usage: python3 live_monitor.py [--once]")
        sys.exit(2)
    sys.exit(run(once="--once" in sys.argv))
