"""syswatch tests: parsers, health rules, panel rendering, and a tmux smoke test of the real UI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]  # kali-utils root
sys.path.insert(0, str(ROOT))

from syswatch import collect, ui  # noqa: E402
from syswatch.cli import summary  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_proc_stat(self):
        text = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 10 0 5 80 5 0 0 0\nintr 1 2 3\n"
        stats = collect.parse_proc_stat(text)
        self.assertEqual(stats["cpu"], (150, 1000))
        self.assertEqual(stats["cpu0"], (15, 100))
        self.assertNotIn("intr", stats)

    def test_meminfo(self):
        m = collect.parse_meminfo("MemTotal:       1000 kB\nMemAvailable:    600 kB\nHugePages_Total:       0\n")
        self.assertEqual((m["MemTotal"], m["MemAvailable"], m["HugePages_Total"]), (1024000, 614400, 0))

    def test_net_dev(self):
        text = ("Inter-|   Receive |  Transmit\n face |bytes packets errs drop fifo frame compressed multicast|...\n"
                "  eno1: 1000 10 1 2 0 0 0 0 2000 20 3 4 0 0 0 0\n")
        n = collect.parse_net_dev(text)["eno1"]
        self.assertEqual((n["rx"], n["rx_errs"], n["rx_drop"], n["tx"], n["tx_errs"], n["tx_drop"]),
                         (1000, 1, 2, 2000, 3, 4))

    def test_diskstats(self):
        text = "   8       0 sda 100 0 2048 50 200 0 4096 80 0 300 130 0 0 0 0\n"
        d = collect.parse_diskstats(text)["sda"]
        self.assertEqual((d["reads"], d["read_sectors"], d["writes"], d["write_sectors"], d["io_ms"]),
                         (100, 2048, 200, 4096, 300))

    def test_gvariant_props(self):
        text = ("({'SmartSupported': <true>, 'SmartUpdated': <uint64 1730000000>, 'SmartTemperature': <310.15>, "
                "'SmartSelftestStatus': <'success'>, 'SmartNumBadSectors': <int64 3>},)")
        p = collect.parse_gvariant_props(text)
        self.assertEqual((p["SmartSupported"], p["SmartUpdated"], p["SmartTemperature"], p["SmartSelftestStatus"],
                          p["SmartNumBadSectors"]), ("true", "1730000000", "310.15", "success", "3"))

    def test_nvidia_csv(self):
        rows = collect.parse_nvidia_csv("0, GeForce GTX 1050 Ti, 37, [N/A], 512\n", ["index", "name", "util", "x", "mem"])
        self.assertEqual(rows, [{"index": "0", "name": "GeForce GTX 1050 Ti", "util": "37", "x": None, "mem": "512"}])


class TestHealth(unittest.TestCase):
    def test_boot_errors_are_informational(self):
        level, reasons = collect.health_verdict("HDD", {"state": "running", "ioerr_cnt": 4}, None, [], None)
        self.assertEqual(level, "ok")
        self.assertIn("since boot", reasons[-1])

    def test_kernel_log_and_heat_warn_but_command_counter_does_not(self):
        level, _ = collect.health_verdict("HDD", {"state": "running", "ioerr_cnt": 60}, None, [], None)
        self.assertEqual(level, "ok")  # smartctl passthrough bumps this counter; it's not a disk error
        level, reasons = collect.health_verdict("HDD", {"state": "running", "ioerr_cnt": 6}, None,
                                                ["I/O error, dev sda"], 58)
        self.assertEqual(level, "warn")
        self.assertEqual(len([r for r in reasons if "informational" not in r]), 2)

    def test_smart_findings_are_listed(self):
        data = {"ata_smart_attributes": {"table": [
            {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 25}},
            {"id": 9, "name": "Power_On_Hours", "raw": {"value": 1034}},
            {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 0}},
            {"id": 3, "name": "Spin_Up_Time", "value": 20, "thresh": 21, "when_failed": "now", "raw": {"value": 1}}]}}
        f = collect.critical_findings(data)
        self.assertEqual([(x["text"], x["level"]) for x in f],
                         [("25 reallocated sectors", "warn"),
                          ("Spin_Up_Time is failing now (value 20 ≤ threshold 21)", "fail")])
        level, reasons = collect.health_verdict("SSD", {"state": "running"},
                                                {"available": True, "failing": False, "findings": f}, [], None)
        self.assertEqual(level, "fail")
        self.assertIn("25 reallocated sectors", reasons)

    def test_smart_failure_and_offline_device(self):
        self.assertEqual(collect.health_verdict("SSD", {"state": "running"},
                                                {"available": True, "failing": True}, [], None)[0], "fail")
        self.assertEqual(collect.health_verdict("SSD", {"state": "offline"}, None, [], None)[0], "fail")


class TestVanishedDrives(unittest.TestCase):
    def sysfs(self, tmp, *names, removable=()):
        for name in names:
            p = Path(tmp, "block", name, "device")
            p.mkdir(parents=True)
            Path(tmp, "block", name, "size").write_text("1000\n")
            Path(tmp, "block", name, "removable").write_text("1\n" if name in removable else "0\n")
            (p / "model").write_text("M\n")

    def collector(self, tmp):
        with mock.patch.object(collect, "SYS", tmp), mock.patch.object(collect, "disk_kind", lambda n: ("SSD", "sata")):
            return collect.Collector(slow=False)

    def refresh(self, c, tmp):
        with mock.patch.object(collect, "SYS", tmp), mock.patch.object(collect, "disk_kind", lambda n: ("SSD", "sata")):
            c.refresh_disks()

    def test_a_fixed_drive_that_disappears_stays_listed_and_is_marked_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.sysfs(tmp, "sda", "sdb")
            c = self.collector(tmp)
            shutil.rmtree(Path(tmp, "block", "sdb"))
            self.refresh(c, tmp)
            self.assertEqual([(d["name"], d.get("gone", False)) for d in c.disks], [("sda", False), ("sdb", True)])

    def test_a_removable_drive_just_goes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.sysfs(tmp, "sda", "sdc", removable=("sdc",))
            c = self.collector(tmp)
            shutil.rmtree(Path(tmp, "block", "sdc"))
            self.refresh(c, tmp)
            self.assertEqual([d["name"] for d in c.disks], ["sda"])

    def test_a_drive_plugged_in_later_appears_and_a_returning_one_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.sysfs(tmp, "sda")
            c = self.collector(tmp)
            self.sysfs(tmp, "sdb")
            self.refresh(c, tmp)
            self.assertEqual([d["name"] for d in c.disks], ["sda", "sdb"])
            shutil.rmtree(Path(tmp, "block", "sdb"))
            self.refresh(c, tmp)
            self.sysfs(tmp, "sdb")
            self.refresh(c, tmp)
            self.assertEqual([d.get("gone", False) for d in c.disks], [False, False])

    def test_a_gone_drive_is_failing_whatever_its_cached_data_says(self):
        level, reasons = collect.health_verdict("SSD", {"state": None, "gone": True}, {"available": True, "findings": []}, [], None)
        self.assertEqual(level, "fail")
        self.assertIn("disappeared", " ".join(reasons))

    def test_the_serial_falls_back_to_the_wwid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.sysfs(tmp, "sda")
            Path(tmp, "block", "sda", "device", "wwid").write_text("t10.ATA     M   S123\n")
            with mock.patch.object(collect, "SYS", tmp), mock.patch.object(collect, "disk_kind", lambda n: ("SSD", "sata")):
                self.assertEqual(collect.block_devices()[0]["serial"], "t10.ATA     M   S123")


class TestStuckMounts(unittest.TestCase):
    MOUNTS = "/dev/sda1 /ok ext4 rw 0 0\n//nas/share /stuck cifs rw 0 0\n"

    def run_with(self, tmp, statvfs):
        Path(tmp, "mounts").write_text(self.MOUNTS)
        with mock.patch.object(collect, "PROC", tmp), mock.patch.object(collect.os, "statvfs", statvfs):
            started = time.monotonic()
            found = collect.filesystems()
            return found, time.monotonic() - started

    def test_a_hung_mount_cannot_freeze_the_dashboard(self):
        release = threading.Event()
        self.addCleanup(release.set)
        real = os.statvfs("/")

        def statvfs(path):
            if path == "/stuck":
                release.wait(20)
            return real
        with tempfile.TemporaryDirectory() as tmp:
            found, took = self.run_with(tmp, statvfs)
            self.assertLess(took, 3)
            self.assertEqual([f["mount"] for f in found], ["/ok"])
            _, again = self.run_with(tmp, statvfs)   # it isn't waited for on every refresh
            self.assertLess(again, 0.5)
            release.set()
            time.sleep(0.2)
            found, _ = self.run_with(tmp, lambda p: real)
            self.assertEqual(sorted(f["mount"] for f in found), ["/ok", "/stuck"])    # back once it answers


class TestDrivePolling(unittest.TestCase):
    def test_should_poll_rules(self):
        self.assertTrue(collect.should_poll(True, 100, None, 5, 600))              # never read yet
        self.assertFalse(collect.should_poll(False, 100, (0, 5), 9, 600))          # too soon
        self.assertFalse(collect.should_poll(True, 700, (0, 5), 5, 600))           # idle HDD: leave it alone
        self.assertTrue(collect.should_poll(True, 700, (0, 5), 6, 600))            # HDD did work: it's awake
        self.assertTrue(collect.should_poll(False, 700, (0, 5), 5, 600))           # SSDs have nothing to wake
        self.assertTrue(collect.should_poll(True, 100, (0, 5), 5, 600, force=True))

    def fake_sys(self, tmp):
        sysfs = Path(tmp)
        (sysfs / "hwmon_dev" / "block" / "sda").mkdir(parents=True)
        for name, dev, reading in (("drivetemp", "hwmon_dev", "36000"), ("coretemp", None, "41000")):
            chip = sysfs / "class" / "hwmon" / ("hwmon0" if dev else "hwmon1")
            chip.mkdir(parents=True)
            (chip / "name").write_text(name + "\n")
            (chip / "temp1_input").write_text(reading + "\n")
            if dev:
                (chip / "device").symlink_to(sysfs / dev)
        return str(sysfs)

    def test_regular_samples_never_touch_drivetemp(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(collect, "SYS", self.fake_sys(tmp)):
            self.assertEqual([t["chip"] for t in collect.hwmon_temps()], ["coretemp"])
            self.assertEqual([t["chip"] for t in collect.hwmon_temps(drive_temps=True)], ["drivetemp"])
            self.assertEqual(collect.hwmon_temps(drive_temps=True, only_disks={"sdb"}), [])

    def test_idle_hdd_gets_no_smart_queries_until_it_works_or_is_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            stats = lambda n: (proc / "diskstats").write_text(f"   8 0 sda {n} 0 8 1 0 0 0 0 0 1 1 0 0 0 0\n")
            stats(10)
            os.makedirs(tmp + "/sys/block/sda")
            Path(tmp, "sys/block/sda/size").write_text("1000\n")
            with mock.patch.object(collect, "PROC", str(proc)), \
                    mock.patch.object(collect, "SYS", self.fake_sys(tmp + "/sys")), \
                    mock.patch.object(collect, "disk_kind", lambda name: ("HDD", "sata")), \
                    mock.patch.object(collect, "smartctl_health", return_value={"available": True}) as smart, \
                    mock.patch.object(collect, "journal_disk_errors", return_value={}), \
                    mock.patch.object(collect, "run", return_value=None):
                c = collect.Collector(slow=False)
                c.pci = []
                c.disks = [{"name": "sda", "kind": "HDD", "model": "x", "size": 1, "transport": "sata", "serial": ""}]
                c.collect_slow(now=1000)
                self.assertEqual(smart.call_count, 1)                # first read
                self.assertEqual(c.drive_temps["sda"][0]["temp"], 36.0)
                c.collect_slow(now=5000)
                self.assertEqual(smart.call_count, 1)                # idle: not touched, cached values stay
                self.assertEqual(c.slow["health"]["sda"]["smart"], {"available": True})
                stats(11)
                c.collect_slow(now=5100)
                self.assertEqual(smart.call_count, 2)                # it did work, so it's awake anyway
                c.refresh_slow()
                c.collect_slow(now=5101)
                self.assertEqual(smart.call_count, 3)                # r: read now


class TestFormatting(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(ui.bar(0.5, 10), "█████·····")
        self.assertEqual(ui.sparkline([0, 50, 100], 5, 100), "    ▄█"[-5:].rjust(5))
        self.assertEqual(ui.sparkline([0, 50, 100], 3, 100), " ▄█")
        self.assertEqual(ui.short_rate(0), "0")
        self.assertEqual(ui.short_rate(1536), "1.5K/s")
        self.assertEqual(ui.duration(90061), "1d 1h")
        self.assertEqual(ui.trunc("abcdef", 4), "abc…")


def fake_snapshot() -> dict:
    return {
        "system": {"hostname": "box", "os": "TestOS", "kernel": "6.0", "cpu_model": "Intel(R) Core(TM) i7 CPU",
                   "cpu_count": 4, "uptime": 3700, "load": [0.5, 0.4, 0.3], "procs": "1/99"},
        "cpu": {"total": 42.0, "cores": [10.0, 95.0, 50.0, 13.0], "freq_mhz": 3400, "freq_max": 3900, "temp": 61.0},
        "memory": {"total": 16 * 2 ** 30, "used": 8 * 2 ** 30, "available": 8 * 2 ** 30, "cached": 2 ** 30,
                   "buffers": 2 ** 20, "swap_total": 0, "swap_used": 0},
        "gpus": [{"name": "GeForce GTX 1050 Ti", "util": 37.0, "vram_used": 2 ** 30, "vram_total": 4 * 2 ** 30,
                  "temp": 55.0, "power": 20.0, "power_limit": 75.0, "fan": 30.0, "clock": 1392, "mem_clock": 3504,
                  "driver": "580.1", "pstate": "P0", "vendor": "nvidia"}],
        "gpu_procs": [{"pid": 1234, "name": "Plex Transcoder", "vram": 300 * 2 ** 20}],
        "network": [{"name": "lo", "rx_rate": 0, "tx_rate": 0, "rx": 1, "tx": 1, "rx_errs": 0, "tx_errs": 0,
                     "rx_drop": 0, "tx_drop": 0, "rx_packets": 1, "tx_packets": 1, "state": "unknown", "speed": None,
                     "wireless": False, "virtual": True},
                    {"name": "eno1", "rx_rate": 5e6, "tx_rate": 1e5, "rx": 4e10, "tx": 1e9, "rx_errs": 0, "tx_errs": 0,
                     "rx_drop": 5, "tx_drop": 0, "rx_packets": 10, "tx_packets": 5, "state": "up", "speed": 1000,
                     "wireless": False, "virtual": False}],
        "addresses": {"eno1": ["192.168.1.128/24"]},
        "disks": [{"name": "sda", "model": "WDC WD20EZBX", "size": 2e12, "kind": "HDD", "transport": "sata",
                   "serial": "X", "read_rate": 1e6, "write_rate": 0, "busy": 12.0, "temp": 38.0, "health": "warn",
                   "health_reasons": ["2 new I/O error(s) while watching"], "kernel": {"state": "running",
                   "ioerr_cnt": 6, "iodone_cnt": 1000, "iotmo_cnt": 0}, "smart": None, "journal": ["I/O error, dev sda"]}],
        "filesystems": [{"mount": "/mnt/data", "device": "/dev/sda1", "fstype": "ext4", "total": 2e12, "used": 1.9e12,
                         "free": 1e11, "disk": "sda", "inodes_used_pct": 1}],
        "temps": [{"chip": "coretemp", "label": "Package id 0", "temp": 61.0, "max": 80.0, "crit": 100.0}],
    }


def text(lines) -> str:
    return "\n".join("".join(t for t, _ in line) for line in lines)


class TestPanels(unittest.TestCase):
    def test_panels_render_and_fit(self):
        s = fake_snapshot()
        hist = {"cpu": [10, 50, 90], "rx:eno1": [1, 2, 3]}
        for width in (40, 76, 120):
            for lines in (ui.cpu_panel(s, hist, width), ui.memory_panel(s, hist, width), ui.gpu_panel(s, width),
                          ui.network_panel(s, hist, width, False), ui.disks_panel(s, width),
                          ui.disk_detail_lines(s, hist, width)):
                self.assertTrue(lines)
        gpu = text(ui.gpu_panel(s, 76))
        self.assertIn("VRAM", gpu)
        self.assertIn("1.0 GiB / 4.0 GiB", gpu)
        self.assertIn("Plex Transcoder", gpu)
        net = text(ui.network_panel(s, hist, 76, False))
        self.assertIn("eno1", net)
        self.assertNotIn("lo ", net)
        self.assertIn("lo", text(ui.network_panel(s, hist, 76, True)))
        disks = text(ui.disks_panel(s, 76))
        self.assertIn("! warning", disks)
        self.assertIn("/mnt/data", disks)
        self.assertIn("I/O error, dev sda", text(ui.disk_detail_lines(s, hist, 120)))

    def test_gpu_note_and_summary(self):
        s = fake_snapshot()
        s["gpus"] = [{"name": "GP107", "vendor": "nvidia", "note": "driver not loaded yet"}]
        self.assertIn("driver not loaded yet", text(ui.gpu_panel(s, 60)))
        out = summary(s)
        self.assertIn("CPU    42.0%", out)
        self.assertIn("/mnt/data", out)

    def test_report_popup_content(self):
        s = fake_snapshot()
        d = s["disks"][0]
        rep = {"name": "sda", "mode": "full", "basic_available": True, "full_available": True, "time": 0,
               "kernel": d["kernel"], "journal": [], "smart": {
                   "smart_status": {"passed": True}, "model_family": "WD Blue", "model_name": "WDC WD20EZBX",
                   "serial_number": "WD-1", "firmware_version": "01", "user_capacity": {"bytes": 2000398934016},
                   "logical_block_size": 512, "physical_block_size": 4096, "rotation_rate": 7200,
                   "sata_version": {"string": "SATA 3.1"},
                   "interface_speed": {"max": {"string": "6.0 Gb/s"}, "current": {"string": "3.0 Gb/s"}},
                   "power_on_time": {"hours": 5830}, "power_cycle_count": 846, "temperature": {"current": 36},
                   "ata_sct_status": {"temperature": {"lifetime_min": 20, "lifetime_max": 45, "power_cycle_min": 30,
                                                      "power_cycle_max": 40}},
                   "ata_device_statistics": {"pages": [{"table": [{"name": "Logical Sectors Written", "value": 2 ** 31}]}]},
                   "ata_smart_attributes": {"table": [
                       {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "thresh": 5,
                        "raw": {"value": 25, "string": "25"}, "flags": {"prefailure": True}},
                       {"id": 193, "name": "Load_Cycle_Count", "value": 195, "worst": 195, "thresh": 0,
                        "raw": {"value": 15790, "string": "15790"}, "flags": {}}]},
                   "ata_smart_self_test_log": {"standard": {"table": [
                       {"type": {"string": "Short offline"}, "status": {"string": "Completed without error"},
                        "lifetime_hours": 5800}]}},
                   "ata_smart_error_log": {"summary": {"count": 0}}}}
        rep["findings"] = collect.critical_findings(rep["smart"])
        out = text(ui.report_lines(rep, d, s["filesystems"], 110))
        for expected in ("SMART self-assessment: PASSED", "25 reallocated sectors", "WD Blue", "7200 rpm hard disk",
                         "link 3.0 Gb/s of max 6.0 Gb/s", "running below max speed", "5,830 h", "Data written",
                         "1.0 TiB", "lifetime 20–45°C", "Head load cycles", "Reallocated_Sector_Ct",
                         "Completed without error", "0 error(s) logged", "no disk errors in the kernel log",
                         "/mnt/data", "smartctl -x"):
            self.assertIn(expected, out)
        rep.update(mode="basic", full_available=False)
        self.assertIn("need the updated enable script", text(ui.report_lines(rep, d, [], 110)))
        rep.update(smart=None, basic_available=False, findings=[], mode="")
        self.assertIn("SMART data needs root", text(ui.report_lines(rep, d, [], 110)))

    def test_processes_sorting(self):
        procs = [{"pid": 1, "user": "root", "cpu": 1.0, "rss": 500, "state": "S", "name": "a"},
                 {"pid": 2, "user": "kali", "cpu": 50.0, "rss": 10, "state": "R", "name": "b"}]
        by_cpu = text(ui.process_lines(procs, 1000, 80, "cpu", 10)).splitlines()
        by_mem = text(ui.process_lines(procs, 1000, 80, "mem", 10)).splitlines()
        self.assertTrue(by_cpu[1].strip().startswith("2"))
        self.assertTrue(by_mem[1].strip().startswith("1"))


class TestCollectorLive(unittest.TestCase):
    """Runs against this machine's real /proc and /sys (read-only)."""

    def test_two_samples(self):
        c = collect.Collector(slow=False)
        c.sample()
        time.sleep(0.2)
        s = c.sample()
        self.assertGreater(s["memory"]["total"], 0)
        self.assertEqual(len(s["cpu"]["cores"]), os.cpu_count())
        self.assertTrue(0 <= s["cpu"]["total"] <= 100)
        self.assertTrue(s["filesystems"])
        self.assertTrue(collect.ProcessSampler().sample())


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestUISmoke(unittest.TestCase):
    def test_views(self):
        session = f"syswatch-test-{os.getpid()}"
        self.state = tempfile.mkdtemp()   # keep the user's real alert history untouched
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "150", "-y", "45",
                        f"XDG_STATE_HOME='{self.state}' {sys.executable} '{ROOT / 'bin' / 'syswatch'}'"], check=True)
        try:
            def screen():
                return subprocess.run(["tmux", "capture-pane", "-p", "-t", session], capture_output=True,
                                      text=True).stdout

            def wait(t):
                end = time.time() + 10
                while time.time() < end:
                    if t in screen():
                        return
                    time.sleep(0.2)
                self.fail(f"{t!r} not on screen:\n{screen()}")
            wait("Memory")
            for k, t in (("2", "Disks, health & sensors"), ("Enter", "Esc close"), ("Escape", "⏎ full report"),
                         ("3", "Network"), ("4", "Processes ("), ("5", "Active ("), ("?", "Keys")):
                subprocess.run(["tmux", "send-keys", "-t", session, k], check=True)
                wait(t)
            subprocess.run(["tmux", "send-keys", "-t", session, "Escape", "q"], check=True)
            time.sleep(0.5)
            self.assertNotIn(session, subprocess.run(["tmux", "ls"], capture_output=True, text=True).stdout)
        finally:
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
            shutil.rmtree(self.state, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
