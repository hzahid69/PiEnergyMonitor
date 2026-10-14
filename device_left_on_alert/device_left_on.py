"""Check if a device stays ON for too long ."""

import csv
import sys
from datetime import datetime

from settings import POWER_LIMIT, TIME_LIMIT, MAX_GAP


def read_time(text):
    """Convert a timestamp from CSV into a Python datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class DeviceChecker:
    def __init__(self):
        self.start_time = None
        self.last_time = None
        self.alert_sent = False

    def check(self, time, power, allow_alert=True):
        """Return True when a new alert should be sent."""
        if self.last_time is not None:
            gap = (time - self.last_time).total_seconds()
            if gap <= 0:              # Ignore duplicate or older samples
                return False
            if gap > MAX_GAP:         # Missing data: start again
                self.start_time = None
                self.alert_sent = False

        self.last_time = time

        if power < POWER_LIMIT:
            self.start_time = None
            self.alert_sent = False
            return False

        if self.start_time is None:
            self.start_time = time

        minutes = (time - self.start_time).total_seconds() / 60
        if minutes >= TIME_LIMIT and not self.alert_sent and allow_alert:
            self.alert_sent = True
            return True
        return False


def check_csv(filename):
    """Test the program with a saved CSV before connecting to InfluxDB."""
    with open(filename, encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    rows.sort(key=lambda row: row["timestamp"])
    checker = DeviceChecker()
    alerts = 0

    for row in rows:
        time = read_time(row["timestamp"])
        power = float(row["power_w"])
        if checker.check(time, power):
            print("ALERT: Device may have been left ON!")
            print("Time:", time, "Power:", power, "W")
            alerts += 1

    print("Total alerts:", alerts)
    return alerts


if __name__ == "__main__":
    filename = sys.argv[1] if len(sys.argv) > 1 else "samples/monitorON_real.csv"
    check_csv(filename)
