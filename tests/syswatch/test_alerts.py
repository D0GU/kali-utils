"""Alert rules, hysteresis, acknowledgement and persistence. Uses fake snapshots and a temp state file."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # kali-utils root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from syswatch import ui  # noqa: E402
from syswatch.alerts import DEFAULTS, AlertEngine, load_thresholds  # noqa: E402
from test_syswatch import fake_snapshot, text  # noqa: E402

GB = 1024 ** 3


def calm() -> dict:
    s = fake_snapshot()
    s["cpu"].update(total=10.0, temp=45.0)
    s["system"]["load"] = [0.5, 0.5, 0.5]
    s["memory"].update(used=4 * GB, available=12 * GB)
    s["gpus"][0].update(temp=50.0, vram_used=GB)
    s["filesystems"][0].update(used=1e12, free=1e12)
    d = s["disks"][0]
    d.update(health="ok", health_reasons=["no problems reported"], temp=35.0, serial="WD-1",
             smart={"available": True, "findings": []})
    return s


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "alerts.json")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, **kw):
        return AlertEngine(dict(DEFAULTS), self.path, **kw)


class TestRules(EngineCase):
    def test_calm_system_has_no_alerts(self):
        e = self.engine()
        self.assertEqual(e.evaluate(calm(), 1000), [])
        self.assertEqual(e.counts(), {"crit": 0, "warn": 0, "info": 0})

    def test_cpu_needs_to_be_sustained_and_clears_with_hysteresis(self):
        e = self.engine()
        s = calm()
        s["cpu"]["total"] = 95.0
        self.assertEqual(e.evaluate(s, 1000), [])           # just started
        self.assertEqual(e.evaluate(s, 1030), [])           # 30 s < 60 s
        (a,) = e.evaluate(s, 1061)
        self.assertEqual((a.key, a.level), ("cpu:usage", "warn"))
        s["cpu"]["total"] = 80.0                             # below trigger, above clear threshold (75)
        e.evaluate(s, 1070)
        self.assertIn("cpu:usage", e.active)
        s["cpu"]["total"] = 20.0
        e.evaluate(s, 1080)
        self.assertNotIn("cpu:usage", e.active)
        self.assertEqual(e.history[0].key, "cpu:usage")
        self.assertEqual(e.history[0].resolved, 1080)

    def test_short_spike_never_alerts(self):
        e = self.engine()
        s = calm()
        s["cpu"]["total"] = 99.0
        e.evaluate(s, 1000)
        s["cpu"]["total"] = 10.0
        e.evaluate(s, 1010)
        s["cpu"]["total"] = 99.0
        self.assertEqual(e.evaluate(s, 1065), [])            # the timer restarted at 1065

    def test_filesystem_levels_and_small_volumes(self):
        e = self.engine()
        s = calm()
        f = s["filesystems"][0]
        f.update(used=0.92 * f["total"], free=0.08 * f["total"])
        (a,) = e.evaluate(s, 1000)
        self.assertEqual((a.key, a.level), ("fs:/mnt/data", "warn"))
        f.update(used=f["total"] - 1.5 * GB, free=1.5 * GB)   # big volume, < 2 GiB free -> critical
        self.assertEqual(e.evaluate(s, 1001)[0].level, "crit")
        boot = {"mount": "/boot", "device": "/dev/sdb2", "fstype": "ext4", "total": 1.9 * GB, "used": 0.3 * GB,
                "free": 1.6 * GB, "disk": "sdb", "inodes_used_pct": 1}
        s2 = calm()
        s2["filesystems"].append(boot)                      # small /boot with 1.6 GiB free is fine
        self.assertFalse([a for a in self.engine(persist=False).evaluate(s2, 1000) if a.key == "fs:/boot"])

    def test_memory_and_temperatures(self):
        e = self.engine()
        s = calm()
        s["memory"].update(used=15.8 * GB, available=0.2 * GB)
        s["cpu"]["temp"] = 96.0
        s["disks"][0]["temp"] = 52.0
        s["gpus"][0].update(temp=88.0, vram_used=3.9 * GB)
        for t in (1000, 1031):
            e.evaluate(s, t)
        levels = {a.key: a.level for a in e.active.values()}
        self.assertEqual(levels["mem:ram"], "crit")
        self.assertEqual(levels["cpu:temp"], "crit")
        self.assertEqual(levels["disk:WD-1:temp"], "warn")      # HDD warn at 50
        self.assertEqual(levels["gpu:0:temp"], "warn")
        self.assertEqual(levels["gpu:0:vram"], "warn")

    def test_link_down_only_after_being_up(self):
        e = self.engine()
        s = calm()
        e.evaluate(s, 1000)
        s["network"][1]["state"] = "down"
        (a,) = e.evaluate(s, 1001)
        self.assertEqual(a.title, "eno1 link is down")
        s["network"][1]["state"] = "up"
        e.evaluate(s, 1002)
        self.assertNotIn("net:eno1:down", e.active)
        down_from_start = calm()
        down_from_start["network"][1]["state"] = "down"
        self.assertEqual(self.engine(persist=False).evaluate(down_from_start, 1000), [])  # never seen up

    def test_network_error_event(self):
        e = self.engine()
        s = calm()
        e.evaluate(s, 1000)
        s["network"][1]["rx_errs"] = 25
        (a,) = e.evaluate(s, 1010)
        self.assertTrue(a.event)
        self.assertIn("25 new network errors", a.title)


def sick(reallocated: int = 25) -> dict:
    s = calm()
    d = s["disks"][0]
    d.update(health="warn", health_reasons=[f"{reallocated} reallocated sectors",
                                            "4 SCSI command error(s) since boot (includes SMART queries; informational)"],
             smart={"available": True, "findings": [{"id": 5, "name": "Reallocated_Sector_Ct",
                                                     "raw": reallocated, "level": "warn",
                                                     "text": f"{reallocated} reallocated sectors", "why": "bad"}]})
    return s


class TestDisksAndAcks(EngineCase):

    def test_disk_health_ack_persists_until_it_gets_worse(self):
        e = self.engine()
        (a,) = e.evaluate(sick(), 1000)
        self.assertEqual(a.detail, "25 reallocated sectors")     # informational counter left out
        e.ack(a.key)
        self.assertEqual(e.counts()["warn"], 0)
        e2 = self.engine()                                        # restart: still acknowledged
        self.assertEqual(e2.evaluate(sick(), 2000), [])
        self.assertEqual(e2.counts()["warn"], 0)
        new = e2.evaluate(sick(27), 2001)                    # worse: the alert returns + a growth event
        keys = sorted(a.key for a in new)
        self.assertEqual(len(keys), 2)
        self.assertTrue(any(":grew:" in k for k in keys))
        grew = next(a for a in new if a.event)
        self.assertIn("grew 25 → 27", grew.title)
        self.assertEqual(e2.counts()["warn"], 2)

    def test_events_survive_restart_until_acknowledged(self):
        e = self.engine()
        e.evaluate(sick(25), 1000)
        e.evaluate(sick(30), 1001)
        ev = next(a for a in e.active.values() if a.event)
        e2 = self.engine()
        self.assertIn(ev.key, e2.active)
        e2.ack(ev.key)
        self.assertNotIn(ev.key, e2.active)
        self.assertEqual(e2.history[0].key, ev.key)
        data = json.loads(Path(self.path).read_text())
        self.assertEqual(data["events"], [])

    def test_thresholds_config(self):
        cfg = os.path.join(self.tmp.name, "alerts.toml")
        Path(cfg).write_text("cpu_percent = 50\nbogus_key = 1\n")
        th = load_thresholds(cfg)
        self.assertEqual(th["cpu_percent"], 50)
        self.assertNotIn("bogus_key", th)


class TestAlertViews(EngineCase):
    def test_widget_and_tab(self):
        e = self.engine()
        self.assertIn("No active alerts", text(ui.alerts_widget_lines(e, 70)))
        e.evaluate(sick(), 1000)
        widget = text(ui.alerts_widget_lines(e, 70))
        self.assertIn("1 warning", widget)
        self.assertIn("health warning", widget)
        marks: list = []
        lines, items = ui.alerts_tab_lines(e, 100, 0, marks)
        out = text(lines)
        self.assertIn("Active (1)", out)
        self.assertIn("History (0)", out)
        self.assertEqual(len(items), 1)
        e.ack(items[0].key)
        self.assertIn("1 acknowledged", text(ui.alerts_widget_lines(e, 70)))


if __name__ == "__main__":
    unittest.main()
