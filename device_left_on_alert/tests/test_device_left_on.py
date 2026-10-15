import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import device_left_on
import live_monitor


BASE = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)


def check_values(values, step_minutes=1):
    checker = device_left_on.DeviceChecker()
    alerts = []
    for index, power in enumerate(values):
        if checker.check(BASE + timedelta(minutes=index * step_minutes), power):
            alerts.append(index)
    return alerts


class DetectionTests(unittest.TestCase):
    def test_10_minutes_above_limit(self):
        self.assertEqual(check_values([12] * 12), [10])

    def test_no_alert_for_4_minutes(self):
        self.assertEqual(check_values([12] * 5), [])

    def test_no_alert_below_limit(self):
        self.assertEqual(check_values([2] * 12), [])

    def test_second_alert_after_off_period(self):
        self.assertEqual(check_values([12] * 11 + [0] + [12] * 11), [10, 22])

    def test_missing_readings_reset_timer(self):
        checker = device_left_on.DeviceChecker()
        points = [(BASE + timedelta(minutes=i), 12) for i in range(5)]
        points += [(BASE + timedelta(minutes=i), 12) for i in range(10, 16)]
        self.assertFalse(any(checker.check(t, w) for t, w in points))

    def test_duplicate_time_is_ignored(self):
        checker = device_left_on.DeviceChecker()
        for i in range(10):
            self.assertFalse(checker.check(BASE + timedelta(minutes=i), 12))
        self.assertFalse(checker.check(BASE + timedelta(minutes=9), 0))
        self.assertTrue(checker.check(BASE + timedelta(minutes=10), 12))
        self.assertFalse(checker.check(BASE + timedelta(minutes=11), 12))

    def test_old_samples_can_warm_up_without_alert(self):
        checker = device_left_on.DeviceChecker()
        for i in range(11):
            self.assertFalse(checker.check(BASE + timedelta(minutes=i), 12, allow_alert=False))
        self.assertTrue(checker.check(BASE + timedelta(minutes=11), 12))

    def test_real_data_at_3_minutes(self):
        path = Path(__file__).parents[1] / "samples" / "monitorON_real.csv"
        with patch.object(device_left_on, "TIME_LIMIT", 3):
            self.assertEqual(device_left_on.check_csv(str(path)), 1)

    def test_real_data_at_10_minutes(self):
        path = Path(__file__).parents[1] / "samples" / "monitorON_real.csv"
        self.assertEqual(device_left_on.check_csv(str(path)), 0)


EXAMPLE_CSV = (
    "#group,false,false,true,true\n"
    "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string\n"
    "#default,_result,,,,,,\n"
    ",result,table,_start,_stop,_time,_value,_field\n"
    ",,0,,,2026-10-12T20:00:00Z,12,apower\n"
    ",,0,,,2026-10-12T20:01:00Z,230,voltage\n"
    "\n"
    "#group,false,false,true,true\n"
    "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string\n"
    "#default,_result,,,,,,\n"
    ",result,table,_start,_stop,_time,_value,_field\n"
    ",,1,,,2026-10-12T20:01:00Z,12.4,apower\n"
)


class NetworkTests(unittest.TestCase):
    def test_parse_influx_annotated_csv(self):
        result = live_monitor.parse_influx_csv(EXAMPLE_CSV)
        self.assertEqual([watts for _, watts in result], [12, 12.4])

    def test_bad_influx_rows_ignored(self):
        data = (",result,table,_time,_value,_field\n"
                ",,0,2026-10-12T20:01:00Z,nan,apower\n"
                ",,0,2026-10-12T20:02:00Z,-7,apower\n"
                ",,0,2026-10-12T20:03:00Z,9,apower\n")
        self.assertEqual(len(live_monitor.parse_influx_csv(data)), 1)

    def test_fetch_uses_read_only_query(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return EXAMPLE_CSV.encode()

        with patch.object(live_monitor, "ORG", "group13"), \
             patch.object(live_monitor, "BUCKET", "measurements"), \
             patch.object(live_monitor, "urlopen", return_value=Reply()) as mock:
            readings = live_monitor.read_influx("example-token")
            req = mock.call_args[0][0]
            self.assertIn("org=group13", req.full_url)
            self.assertEqual(req.headers["Authorization"], "Token example-token")
            self.assertEqual(req.get_method(), "POST")
            self.assertIn('r._field == "apower"', req.data.decode())
            self.assertEqual(len(readings), 2)

    def test_grafana_annotation_format(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"id":1}'

        with patch.object(live_monitor, "GRAFANA_DASHBOARD_UID", "aBc"), \
             patch.object(live_monitor, "urlopen", return_value=Reply()) as mock:
            live_monitor.add_grafana_marker(BASE, 12, "grafana-token")
            req = mock.call_args[0][0]
            payload = json.loads(req.data.decode())
            self.assertEqual(payload["dashboardUID"], "aBc")
            self.assertEqual(payload["time"], int(BASE.timestamp() * 1000))
            self.assertIn("device-left-on", payload["tags"])
            self.assertEqual(req.headers["Authorization"], "Bearer grafana-token")

    def test_save_alert_csv_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                live_monitor.save_alert(BASE, 12)
                live_monitor.save_alert(BASE + timedelta(minutes=15), 13)
                with open("alerts.csv", newline="") as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["power_w"], "12")
            finally:
                os.chdir(cwd)

    def test_once_never_writes_annotations(self):
        point = [(datetime.now(timezone.utc) + timedelta(seconds=1), 12)]
        env = {"INFLUX_TOKEN": "dummy", "GRAFANA_TOKEN": "dummy"}
        with patch.dict(os.environ, env), \
             patch.object(live_monitor, "ORG", "group13"), \
             patch.object(live_monitor, "BUCKET", "energy"), \
             patch.object(live_monitor, "read_influx", return_value=point), \
             patch.object(live_monitor, "add_grafana_marker") as marker, \
             patch.object(live_monitor, "save_alert") as saved:
            self.assertEqual(live_monitor.run(once=True), 0)
            marker.assert_not_called()
            saved.assert_not_called()


if __name__ == "__main__":
    unittest.main()

class LiveFlowTests(unittest.TestCase):
    def test_live_flow_sends_one_grafana_marker(self):
        now = datetime.now(timezone.utc)
        history = [(now - timedelta(minutes=10-i), 12) for i in range(10)]
        second_poll = history + [(now + timedelta(seconds=2), 12)]
        env = {"INFLUX_TOKEN": "dummy", "GRAFANA_TOKEN": "dummy"}
        with patch.dict(os.environ, env), \
             patch.object(live_monitor, "ORG", "group13"), \
             patch.object(live_monitor, "BUCKET", "energy"), \
             patch.object(live_monitor, "read_influx", side_effect=[history, second_poll]), \
             patch.object(live_monitor, "add_grafana_marker") as marker, \
             patch.object(live_monitor, "save_alert") as saved, \
             patch.object(live_monitor.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            self.assertEqual(live_monitor.run(), 0)
            marker.assert_called_once()
            saved.assert_called_once()

    def test_missing_influx_token_fails_cleanly(self):
        env = dict(os.environ)
        env.pop("INFLUX_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(live_monitor.run(once=True), 2)
