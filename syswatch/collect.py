"""Collect system statistics from /proc, /sys and a few optional tools.

No root needed. Optional sources are used when present:
  nvidia-smi                 NVIDIA GPU usage/VRAM/temperature/power
  gdbus (UDisks2)            SMART health for SATA/NVMe drives
  smartctl                   SMART health when running as root
  journalctl                 kernel I/O errors per disk (user must be in 'adm' or 'systemd-journal')
  ip                         interface addresses
"""

from __future__ import annotations

import glob
import json
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
from collections import deque

PROC = os.environ.get("SYSWATCH_PROC", "/proc")
SYS = os.environ.get("SYSWATCH_SYS", "/sys")
HISTORY = 120
PSEUDO_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs", "pstore", "bpf",
             "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "overlay",
             "squashfs", "nsfs", "ramfs", "efivarfs", "rpc_pipefs", "fuse.portal", "fuse.gvfsd-fuse", "nfsd",
             "fuse.snapfuse", "devfs"}


def read(path: str, default: str = "") -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return default


def run(cmd: list[str], timeout: float = 4) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Parsers (pure functions; unit-tested)
# --------------------------------------------------------------------------

def parse_proc_stat(text: str) -> dict[str, tuple[int, int]]:
    """{'cpu': (busy, total), 'cpu0': ...} in clock ticks."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        vals = [int(v) for v in parts[1:9]] + [0] * max(0, 8 - len(parts[1:9]))
        user, nice, system, idle, iowait, irq, softirq, steal = vals[:8]
        total = user + nice + system + idle + iowait + irq + softirq + steal
        out[parts[0]] = (total - idle - iowait, total)
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields:
            out[key] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
    return out


def parse_net_dev(text: str) -> dict[str, dict]:
    out = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) < 16:
            continue
        out[name.strip()] = {"rx": int(f[0]), "rx_packets": int(f[1]), "rx_errs": int(f[2]), "rx_drop": int(f[3]),
                             "tx": int(f[8]), "tx_packets": int(f[9]), "tx_errs": int(f[10]), "tx_drop": int(f[11])}
    return out


def parse_diskstats(text: str) -> dict[str, dict]:
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        out[f[2]] = {"reads": int(f[3]), "read_sectors": int(f[5]), "writes": int(f[7]),
                     "write_sectors": int(f[9]), "io_ms": int(f[12])}
    return out


def parse_gvariant_props(text: str) -> dict[str, str]:
    """Pull 'Key': <value> pairs out of gdbus GetAll output (scalars only)."""
    return {k: v.strip("'") for k, v in re.findall(r"'(\w+)': <(?:[a-z]+\d* )?([^<>]*?)>", text)}


def parse_nvidia_csv(text: str, fields: list[str]) -> list[dict]:
    rows = []
    for line in text.strip().splitlines():
        vals = [v.strip() for v in line.split(",")]
        if len(vals) != len(fields):
            continue
        row = {}
        for k, v in zip(fields, vals):
            row[k] = None if v in ("[N/A]", "N/A", "[Not Supported]", "") else v
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Static-ish information
# --------------------------------------------------------------------------

def system_info() -> dict:
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', read("/etc/os-release"), re.M))
    u = os.uname()
    cpuinfo = read(f"{PROC}/cpuinfo")
    model = re.search(r"model name\s*:\s*(.+)", cpuinfo)
    return {"hostname": u.nodename, "os": osr.get("PRETTY_NAME", u.sysname), "kernel": u.release,
            "arch": u.machine, "cpu_model": re.sub(r"\s+", " ", model.group(1)) if model else u.machine,
            "cpu_count": os.cpu_count() or 1}


def disk_kind(name: str) -> tuple[str, str]:
    """(kind, transport) for a block device."""
    target = os.path.realpath(f"{SYS}/block/{name}")
    if name.startswith("nvme"):
        transport = "nvme"
    elif "/usb" in target:
        transport = "usb"
    elif "/ata" in target:
        transport = "sata"
    elif "/virtio" in target:
        transport = "virtio"
    else:
        transport = ""
    rotational = read(f"{SYS}/block/{name}/queue/rotational").strip() == "1"
    kind = "NVMe" if transport == "nvme" else "HDD" if rotational else "SSD"
    if transport == "usb":
        kind = "USB " + kind
    return kind, transport


def block_devices() -> list[dict]:
    disks = []
    for path in sorted(glob.glob(f"{SYS}/block/*")):
        name = os.path.basename(path)
        if name.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd")):
            continue
        size = int(read(f"{path}/size", "0").strip() or 0) * 512
        if size == 0:
            continue
        model = (read(f"{path}/device/model") or read(f"{path}/device/name")).strip()
        kind, transport = disk_kind(name)
        disks.append({"name": name, "model": model, "size": size, "kind": kind, "transport": transport,
                      "serial": read(f"{path}/device/serial").strip()})
    return disks


def parent_disk(dev: str) -> str:
    """'sda1' -> 'sda', 'nvme0n1p2' -> 'nvme0n1', LVM/crypt 'dm-0' -> the disk underneath."""
    name = os.path.basename(dev)
    slaves = glob.glob(f"{SYS}/block/{name}/slaves/*")
    if name.startswith(("dm-", "md")) and slaves:
        return parent_disk(os.path.basename(slaves[0]))
    real = os.path.realpath(f"{SYS}/class/block/{name}")
    parent = os.path.basename(os.path.dirname(real))
    return parent if os.path.exists(f"{SYS}/block/{parent}") else name


def filesystems() -> list[dict]:
    out, seen = [], set()
    for line in read(f"{PROC}/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype in PSEUDO_FS or not dev.startswith("/") or mnt.startswith(("/snap", "/boot/efi")):
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        key = (dev, st.f_blocks)
        if key in seen:
            continue
        seen.add(key)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        real = os.path.realpath(dev)
        out.append({"mount": mnt, "device": dev, "fstype": fstype, "total": total, "used": used, "free": free,
                    "disk": parent_disk(real) if real.startswith("/dev/") else "",
                    "inodes_used_pct": 100 * (1 - st.f_ffree / st.f_files) if st.f_files else 0})
    return sorted(out, key=lambda f: f["mount"])


def hwmon_temps() -> list[dict]:
    temps = []
    for chip in sorted(glob.glob(f"{SYS}/class/hwmon/hwmon*")):
        name = read(f"{chip}/name").strip()
        device = os.path.realpath(f"{chip}/device")
        for inp in sorted(glob.glob(f"{chip}/temp*_input")):
            base = inp[:-len("_input")]
            value = num(read(inp).strip())
            if value is None:
                continue
            temps.append({"chip": name, "label": read(f"{base}_label").strip() or os.path.basename(base),
                          "temp": value / 1000, "max": (num(read(f"{base}_max").strip()) or 0) / 1000 or None,
                          "crit": (num(read(f"{base}_crit").strip()) or 0) / 1000 or None, "device": device})
    return temps


def disk_temps(temps: list[dict]) -> dict[str, float]:
    """Drive temperatures from hwmon (drivetemp module for SATA, nvme driver for NVMe)."""
    out = {}
    for t in temps:
        if t["chip"] not in ("drivetemp", "nvme"):
            continue
        blocks = glob.glob(os.path.join(t["device"], "block", "*")) or glob.glob(os.path.join(t["device"], "nvme*n1"))
        for b in blocks:
            out.setdefault(os.path.basename(b), t["temp"])
    return out


# --------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------

NV_FIELDS = ["index", "name", "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
             "temperature.gpu", "power.draw", "power.limit", "fan.speed", "clocks.gr", "clocks.mem",
             "driver_version", "pstate"]


def pci_gpus() -> list[dict]:
    gpus = []
    for dev in sorted(glob.glob(f"{SYS}/bus/pci/devices/*")):
        cls = read(f"{dev}/class").strip()
        if not cls.startswith(("0x0300", "0x0302", "0x0380")):
            continue
        vendor = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}.get(read(f"{dev}/vendor").strip(), "other")
        driver = os.path.basename(os.path.realpath(f"{dev}/driver")) if os.path.exists(f"{dev}/driver") else ""
        addr = os.path.basename(dev)
        name = ""
        out = run(["lspci", "-s", addr], timeout=2)
        if out:
            name = re.sub(r"^\S+\s+[^:]+:\s*", "", out.strip())
            name = re.sub(r"\s*\(rev \w+\)$", "", name)
        gpus.append({"pci": addr, "vendor": vendor, "driver": driver, "name": name or vendor.upper(), "path": dev})
    return gpus


def nvidia_gpus() -> list[dict] | None:
    out = run(["nvidia-smi", f"--query-gpu={','.join(NV_FIELDS)}", "--format=csv,noheader,nounits"], timeout=3)
    if out is None:
        return None
    gpus = []
    for r in parse_nvidia_csv(out, NV_FIELDS):
        gpus.append({"name": r["name"], "util": num(r["utilization.gpu"]), "mem_util": num(r["utilization.memory"]),
                     "vram_used": (num(r["memory.used"]) or 0) * 1024 ** 2,
                     "vram_total": (num(r["memory.total"]) or 0) * 1024 ** 2,
                     "temp": num(r["temperature.gpu"]), "power": num(r["power.draw"]),
                     "power_limit": num(r["power.limit"]), "fan": num(r["fan.speed"]), "clock": num(r["clocks.gr"]),
                     "mem_clock": num(r["clocks.mem"]), "driver": r["driver_version"], "pstate": r["pstate"],
                     "vendor": "nvidia", "source": "nvidia-smi"})
    return gpus


def nvidia_processes() -> list[dict]:
    out = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    procs = []
    for r in parse_nvidia_csv(out or "", ["pid", "name", "mem"]):
        procs.append({"pid": int(r["pid"]), "name": os.path.basename(r["name"] or "?"),
                      "vram": (num(r["mem"]) or 0) * 1024 ** 2})
    return sorted(procs, key=lambda p: -p["vram"])


def sysfs_gpu(g: dict) -> dict:
    """AMD (amdgpu) exposes usage and VRAM in sysfs; others only identity."""
    dev = g["path"]
    info = {"name": g["name"], "vendor": g["vendor"], "driver": g["driver"], "source": "sysfs"}
    busy = num(read(f"{dev}/gpu_busy_percent").strip())
    if busy is not None:
        info["util"] = busy
    used = num(read(f"{dev}/mem_info_vram_used").strip())
    total = num(read(f"{dev}/mem_info_vram_total").strip())
    if total:
        info["vram_used"], info["vram_total"] = used or 0, total
    for chip in glob.glob(f"{dev}/hwmon/hwmon*"):
        t = num(read(f"{chip}/temp1_input").strip())
        if t is not None:
            info["temp"] = t / 1000
        p = num(read(f"{chip}/power1_average").strip()) or num(read(f"{chip}/power1_input").strip())
        if p is not None:
            info["power"] = p / 1e6
    if "util" not in info and "vram_total" not in info:
        if g["vendor"] == "nvidia" and not g["driver"] and shutil.which("nvidia-smi"):
            info["note"] = "The NVIDIA driver is installed but not loaded yet (usually after a reboot)."
        elif g["vendor"] == "nvidia" and g["driver"] == "nouveau":
            info["note"] = ("nouveau driver: no usage stats. Statistics appear once the NVIDIA driver is loaded "
                            "(after a reboot).")
        elif g["vendor"] == "nvidia":
            info["note"] = "nvidia-smi isn't available or the NVIDIA driver isn't loaded."
        elif not g["driver"]:
            info["note"] = "no driver bound to this GPU."
        else:
            info["note"] = f"the {g['driver']} driver doesn't report usage."
    return info


# --------------------------------------------------------------------------
# Disk health
# --------------------------------------------------------------------------

def kernel_disk_health(name: str) -> dict:
    dev = f"{SYS}/block/{name}/device"
    h = {"state": read(f"{dev}/state").strip() or None}
    for key in ("ioerr_cnt", "iodone_cnt", "iotmo_cnt"):
        raw = read(f"{dev}/{key}").strip()
        if raw:
            try:
                h[key] = int(raw, 16) if raw.startswith("0x") else int(raw)
            except ValueError:
                pass
    return h


def udisks_smart(name: str) -> dict | None:
    if not shutil.which("gdbus"):
        return None
    base = ["gdbus", "call", "--system", "--dest", "org.freedesktop.UDisks2"]
    out = run(base + ["--object-path", f"/org/freedesktop/UDisks2/block_devices/{name}", "--method",
                      "org.freedesktop.DBus.Properties.Get", "org.freedesktop.UDisks2.Block", "Drive"], timeout=3)
    m = re.search(r"objectpath '([^']+)'", out or "")
    if not m or m.group(1) == "/":
        return None
    drive = m.group(1)
    for iface in ("org.freedesktop.UDisks2.Drive.Ata", "org.freedesktop.UDisks2.NVMe.Controller"):
        out = run(base + ["--object-path", drive, "--method", "org.freedesktop.DBus.Properties.GetAll", iface],
                  timeout=3)
        if not out:
            continue
        p = parse_gvariant_props(out)
        if iface.endswith("Ata"):
            if p.get("SmartSupported") != "true" or p.get("SmartUpdated", "0") == "0":
                return {"source": "udisks", "available": False}
            temp_k = num(p.get("SmartTemperature"), 0)
            return {"source": "udisks", "available": True, "failing": p.get("SmartFailing") == "true",
                    "temp": temp_k - 273.15 if temp_k else None,
                    "power_on_hours": (num(p.get("SmartPowerOnSeconds"), 0) or 0) / 3600,
                    "bad_sectors": int(num(p.get("SmartNumBadSectors"), -1)),
                    "attrs_failing": int(num(p.get("SmartNumAttributesFailing"), -1)),
                    "attrs_failed_past": int(num(p.get("SmartNumAttributesFailedInThePast"), -1)),
                    "selftest": p.get("SmartSelftestStatus") or ""}
        if p.get("SmartUpdated", "0") == "0":
            return {"source": "udisks", "available": False}
        temp_k = num(p.get("SmartTemperature"), 0)
        return {"source": "udisks", "available": True, "failing": "[]" not in out.split("SmartCriticalWarning")[1][:30]
                if "SmartCriticalWarning" in out else False,
                "temp": temp_k - 273.15 if temp_k else None,
                "power_on_hours": num(p.get("SmartPowerOnHours"), 0),
                "percent_used": num(p.get("SmartPercentUsed")), "bad_sectors": -1}
    return None


SUDOERS_RULE = "/etc/sudoers.d/syswatch-smartctl"        # created by ~/scripts/syswatch-enable-health.sh
SUDOERS_FULL = "/etc/sudoers.d/syswatch-smartctl-full"   # newer script version: also allows `smartctl -j -x`

# SMART attributes worth explaining when their raw value isn't 0: id -> (short label, plain explanation, level)
CRITICAL_ATTRS = {
    5: ("reallocated sectors", "bad sectors the drive has already replaced with spares", "warn"),
    10: ("spin-up retries", "the motor needed more than one try to spin up", "warn"),
    183: ("runtime bad blocks", "blocks that failed while in use", "warn"),
    184: ("end-to-end errors", "data corrupted inside the drive's own cache/path", "fail"),
    187: ("uncorrectable errors", "reads the drive could not recover with error correction", "warn"),
    188: ("command timeouts", "operations that timed out (often power or cable trouble)", "warn"),
    196: ("reallocation events", "times the drive had to remap sectors", "warn"),
    197: ("pending sectors", "unstable sectors waiting to be remapped; may mean unreadable data", "warn"),
    198: ("offline uncorrectable", "sectors found unreadable during the drive's own scans", "warn"),
    199: ("interface CRC errors", "data corrupted on the cable/connector, not the disk itself", "warn"),
}


def smartctl_exe() -> str | None:
    return shutil.which("smartctl") or ("/usr/sbin/smartctl" if os.path.exists("/usr/sbin/smartctl") else None)


def smartctl_json(name: str, full: bool = False) -> tuple[dict | None, str]:
    """Run smartctl in JSON mode. Returns (data, mode) with mode 'full' (-x) or 'basic' (-H -A -i).

    As root anything goes; otherwise only through the exact sudo rules the enable script installs.
    """
    exe = smartctl_exe()
    if not exe:
        return None, ""
    root = os.geteuid() == 0
    if full and (root or os.path.exists(SUDOERS_FULL)):
        cmd, mode = [exe, "-j", "-x", f"/dev/{name}"], "full"
    elif root or os.path.exists(SUDOERS_RULE):
        cmd, mode = [exe, "-j", "-H", "-A", "-i", f"/dev/{name}"], "basic"
    else:
        return None, ""
    if not root:
        cmd = ["sudo", "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout or "{}"), mode
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, ""


def critical_findings(data: dict) -> list[dict]:
    """Non-zero 'bad news' attributes (ATA) or health-log counters (NVMe)."""
    out = []
    for a in (data.get("ata_smart_attributes") or {}).get("table", []):
        raw = (a.get("raw") or {}).get("value", 0)
        if a.get("when_failed"):
            out.append({"id": a["id"], "name": a["name"], "raw": raw, "level": "fail",
                        "text": f"{a['name']} {'is failing now' if a['when_failed'] == 'now' else 'failed in the past'}"
                                f" (value {a.get('value')} ≤ threshold {a.get('thresh')})"})
        elif a["id"] in CRITICAL_ATTRS and raw:
            label, why, level = CRITICAL_ATTRS[a["id"]]
            out.append({"id": a["id"], "name": a["name"], "raw": raw, "level": level,
                        "text": f"{raw:,} {label}", "why": why})
    nv = data.get("nvme_smart_health_information_log") or {}
    if nv.get("critical_warning"):
        out.append({"id": "nvme", "name": "critical_warning", "raw": nv["critical_warning"], "level": "fail",
                    "text": f"NVMe critical warning flags 0x{nv['critical_warning']:x}"})
    if nv.get("media_errors"):
        out.append({"id": "nvme", "name": "media_errors", "raw": nv["media_errors"], "level": "warn",
                    "text": f"{nv['media_errors']:,} media/data integrity errors",
                    "why": "unrecovered data integrity errors reported by the controller"})
    if nv and nv.get("available_spare") is not None and nv.get("available_spare_threshold") is not None \
            and nv["available_spare"] <= nv["available_spare_threshold"]:
        out.append({"id": "nvme", "name": "available_spare", "raw": nv["available_spare"], "level": "fail",
                    "text": f"spare blocks down to {nv['available_spare']}% (threshold {nv['available_spare_threshold']}%)"})
    return out


def smartctl_health(name: str) -> dict | None:
    data, _ = smartctl_json(name)
    if data is None:
        return None
    if "smart_status" not in data:
        return {"source": "smartctl", "available": False}
    attrs = {a["id"]: a for a in (data.get("ata_smart_attributes") or {}).get("table", [])}
    raw = lambda i: (attrs.get(i) or {}).get("raw", {}).get("value", 0)
    nvme = data.get("nvme_smart_health_information_log") or {}
    return {"source": "smartctl", "available": True, "failing": not data["smart_status"].get("passed", True),
            "temp": (data.get("temperature") or {}).get("current"),
            "power_on_hours": (data.get("power_on_time") or {}).get("hours"),
            "bad_sectors": raw(5) + raw(197) + raw(198) if attrs else nvme.get("media_errors", -1),
            "reallocated": raw(5), "pending": raw(197), "uncorrectable": raw(198),
            "percent_used": nvme.get("percentage_used"), "findings": critical_findings(data)}


def disk_report(name: str) -> dict:
    """Everything known about one drive, for the detail popup (may take a second or two)."""
    data, mode = smartctl_json(name, full=True)
    return {"name": name, "smart": data, "mode": mode,
            "full_available": os.geteuid() == 0 or os.path.exists(SUDOERS_FULL),
            "basic_available": os.geteuid() == 0 or os.path.exists(SUDOERS_RULE),
            "findings": critical_findings(data or {}), "kernel": kernel_disk_health(name),
            "journal": journal_disk_errors([name], since="-7 days").get(name, []),
            "udisks": None if data else udisks_smart(name), "time": time.time()}


JOURNAL_ERR_RE = re.compile(r"(?i)(i/o error|error|failed|critical medium|unrecovered read|reset|timeout)")


def journal_disk_errors(names: list[str], since: str = "-24 hours") -> dict[str, list[str]]:
    out = run(["journalctl", "-k", "-p", "err", "--since", since, "--no-pager", "-o", "short-iso"], timeout=8)
    found: dict[str, list[str]] = {n: [] for n in names}
    for line in (out or "").splitlines():
        if not JOURNAL_ERR_RE.search(line):
            continue
        for n in names:
            if re.search(rf"(\[{n}\]|dev {n}\b|\b{n}:|\b{n}\d*\b)", line):
                found[n].append(line.strip())
    return found


def health_verdict(kind: str, kernel: dict, smart: dict | None, journal: list[str], temp: float | None,
                   ioerr_baseline: int | None = None) -> tuple[str, list[str]]:
    """('ok' | 'warn' | 'fail' , reasons)."""
    level, reasons = "ok", []

    def bump(new, why):
        nonlocal level
        order = {"ok": 0, "warn": 1, "fail": 2}
        if order[new] > order[level]:
            level = new
        reasons.append(why)
    if kernel.get("state") and kernel["state"] not in ("running", "live"):
        bump("fail", f"device state: {kernel['state']}")
    if smart and smart.get("available"):
        if smart.get("failing"):
            bump("fail", "SMART says the drive is failing")
        if smart.get("findings") is not None:
            for f in smart["findings"]:
                bump(f["level"], f["text"])
        elif (smart.get("bad_sectors") or 0) > 0:
            bump("warn", f"{smart['bad_sectors']} bad/pending sectors")
        if (smart.get("attrs_failing") or 0) > 0:
            bump("warn", f"{smart['attrs_failing']} SMART attribute(s) failing")
        if (smart.get("percent_used") or 0) >= 90:
            bump("warn", f"{smart['percent_used']:.0f}% of rated wear used")
    errs = kernel.get("ioerr_cnt") or 0
    if errs:
        # SMART queries (smartctl/udisks ATA passthrough) are counted here too, so this is informational;
        # real read/write failures show up in the kernel log, which raises a warning above.
        reasons.append(f"{errs} SCSI command error(s) since boot (includes SMART queries; informational)")
    if journal:
        bump("warn", f"{len(journal)} kernel error message(s) in the last 24 h")
    limit = 55 if "HDD" in kind else 70
    if temp is not None and temp >= limit:
        bump("warn", f"hot: {temp:.0f}°C")
    if level == "ok":
        reasons.insert(0, "no problems reported" + ("" if smart and smart.get("available") else
                                                    " (kernel counters and log only; SMART unavailable)"))
    return level, reasons


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def read_processes() -> dict[int, dict]:
    procs = {}
    users: dict[int, str] = {}
    for d in os.listdir(PROC):
        if not d.isdigit():
            continue
        stat = read(f"{PROC}/{d}/stat")
        if not stat:
            continue
        try:
            head, _, rest = stat.rpartition(")")
            name = head.split("(", 1)[1]
            f = rest.split()
            ticks = int(f[11]) + int(f[12])
            rss = int(f[21]) * PAGE
            uid = os.stat(f"{PROC}/{d}").st_uid
        except (IndexError, ValueError, OSError):
            continue
        if uid not in users:
            try:
                users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                users[uid] = str(uid)
        cmd = read(f"{PROC}/{d}/cmdline").replace("\x00", " ").strip()
        if cmd:  # the kernel truncates `name` to 15 chars; prefer the real command line
            argv0, _, args = cmd.partition(" ")
            cmd = (os.path.basename(argv0) + (" " + args if args else "")).strip()
        procs[int(d)] = {"pid": int(d), "name": cmd or f"[{name}]", "ticks": ticks, "rss": rss, "user": users[uid],
                         "state": f[0]}
    return procs


# --------------------------------------------------------------------------
# The collector
# --------------------------------------------------------------------------

class Collector:
    """Call sample() periodically; slow sources refresh in a background thread."""

    def __init__(self, slow: bool = True):
        self.info = system_info()
        self.prev: dict = {}
        self.prev_time = None
        self.history: dict[str, deque] = {}
        self.slow: dict = {"gpus": [], "gpu_procs": [], "health": {}, "journal": {}, "addresses": {},
                           "updated": 0.0}
        self.slow_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.pci = pci_gpus()
        self.disks = block_devices()
        self.ioerr_baseline = {d["name"]: kernel_disk_health(d["name"]).get("ioerr_cnt") or 0 for d in self.disks}
        if slow:
            threading.Thread(target=self._slow_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh_slow(self) -> None:
        self._wake.set()

    def _hist(self, key: str, value: float) -> None:
        self.history.setdefault(key, deque(maxlen=HISTORY)).append(value)

    # -- slow sources ------------------------------------------------------------

    def collect_slow(self) -> None:
        names = [d["name"] for d in self.disks]
        nv = nvidia_gpus() if any(g["vendor"] == "nvidia" for g in self.pci) else None
        gpus = nv if nv else [sysfs_gpu(g) for g in self.pci]
        procs = nvidia_processes() if nv else []
        health = {}
        for n in names:
            smart = smartctl_health(n) or udisks_smart(n)
            health[n] = {"kernel": kernel_disk_health(n), "smart": smart}
        journal = journal_disk_errors(names)
        addresses = {}
        out = run(["ip", "-j", "addr"], timeout=3)
        if out:
            try:
                for iface in json.loads(out):
                    addresses[iface["ifname"]] = [f"{a['local']}/{a['prefixlen']}" for a in iface.get("addr_info", [])
                                                  if a.get("family") in ("inet", "inet6")
                                                  and a.get("scope") != "link"]
            except (ValueError, KeyError):
                pass
        with self.slow_lock:
            self.slow = {"gpus": gpus, "gpu_procs": procs, "health": health, "journal": journal,
                         "addresses": addresses, "updated": time.time()}

    def _slow_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect_slow()
            except Exception:  # keep the UI alive whatever a tool prints
                pass
            self._wake.wait(30)
            self._wake.clear()

    def fast_gpu(self) -> None:
        """nvidia-smi is quick enough to poll every sample for live usage."""
        if any(g["vendor"] == "nvidia" for g in self.pci):
            nv = nvidia_gpus()
            if nv:
                with self.slow_lock:
                    self.slow["gpus"] = nv
        elif any(g["vendor"] == "amd" for g in self.pci):
            with self.slow_lock:
                self.slow["gpus"] = [sysfs_gpu(g) for g in self.pci]

    # -- one sample ------------------------------------------------------------------

    def sample(self) -> dict:
        now = time.monotonic()
        dt = (now - self.prev_time) if self.prev_time else None
        prev = self.prev
        cur: dict = {}

        # CPU
        cpu = parse_proc_stat(read(f"{PROC}/stat"))
        cur["cpu"] = cpu
        usage = {}
        for k, (busy, total) in cpu.items():
            pb, pt = (prev.get("cpu") or {}).get(k, (busy, total))
            usage[k] = 100 * (busy - pb) / (total - pt) if total > pt else 0.0
        freqs = [num(read(p).strip()) for p in glob.glob(f"{SYS}/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
        freqs = [f / 1000 for f in freqs if f]
        if not freqs:
            freqs = [float(x) for x in re.findall(r"cpu MHz\s*:\s*([\d.]+)", read(f"{PROC}/cpuinfo"))]
        temps = hwmon_temps()
        pkg = [t for t in temps if t["chip"] in ("coretemp", "k10temp", "zenpower", "cpu_thermal")]
        cpu_temp = max((t["temp"] for t in pkg), default=None)
        load = read(f"{PROC}/loadavg").split()
        self._hist("cpu", usage.get("cpu", 0.0))

        # memory
        mem = parse_meminfo(read(f"{PROC}/meminfo"))
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        memory = {"total": total, "used": total - avail, "available": avail,
                  "cached": mem.get("Cached", 0) + mem.get("SReclaimable", 0), "buffers": mem.get("Buffers", 0),
                  "swap_total": mem.get("SwapTotal", 0),
                  "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)}
        self._hist("mem", 100 * memory["used"] / total if total else 0)

        # network
        net = parse_net_dev(read(f"{PROC}/net/dev"))
        cur["net"] = net
        interfaces = []
        for name, c in net.items():
            p = (prev.get("net") or {}).get(name)
            rx = (c["rx"] - p["rx"]) / dt if p and dt else 0.0
            tx = (c["tx"] - p["tx"]) / dt if p and dt else 0.0
            self._hist(f"rx:{name}", max(0.0, rx))
            self._hist(f"tx:{name}", max(0.0, tx))
            speed = num(read(f"{SYS}/class/net/{name}/speed").strip())
            interfaces.append({"name": name, "rx_rate": max(0.0, rx), "tx_rate": max(0.0, tx), **c,
                               "state": read(f"{SYS}/class/net/{name}/operstate").strip() or "?",
                               "speed": speed if speed and speed > 0 else None,
                               "wireless": os.path.exists(f"{SYS}/class/net/{name}/wireless"),
                               "virtual": os.path.realpath(f"{SYS}/class/net/{name}").startswith(
                                   f"{SYS}/devices/virtual") if os.path.exists(f"{SYS}/class/net/{name}") else False})

        # disks
        ds = parse_diskstats(read(f"{PROC}/diskstats"))
        cur["disk"] = ds
        dtemps = disk_temps(temps)
        with self.slow_lock:
            slow = dict(self.slow)
        disks = []
        for d in self.disks:
            c = ds.get(d["name"])
            p = (prev.get("disk") or {}).get(d["name"])
            r = w = busy = 0.0
            if c and p and dt:
                r = (c["read_sectors"] - p["read_sectors"]) * 512 / dt
                w = (c["write_sectors"] - p["write_sectors"]) * 512 / dt
                busy = min(100.0, (c["io_ms"] - p["io_ms"]) / (dt * 10))
            self._hist(f"io:{d['name']}", r + w)
            h = slow["health"].get(d["name"], {"kernel": kernel_disk_health(d["name"]), "smart": None})
            temp = dtemps.get(d["name"])
            if temp is None and h.get("smart") and h["smart"].get("temp"):
                temp = h["smart"]["temp"]
            journal = slow["journal"].get(d["name"], [])
            level, reasons = health_verdict(d["kind"], kernel_disk_health(d["name"]), h.get("smart"), journal, temp,
                                            self.ioerr_baseline.get(d["name"]))
            disks.append({**d, "read_rate": r, "write_rate": w, "busy": busy, "temp": temp, "health": level,
                          "health_reasons": reasons, "kernel": h["kernel"], "smart": h.get("smart"),
                          "journal": journal[-5:]})

        uptime = num(read(f"{PROC}/uptime").split()[0] if read(f"{PROC}/uptime") else 0, 0)
        self.prev, self.prev_time = cur, now
        return {
            "time": time.time(), "interval": dt, "system": {**self.info, "uptime": uptime,
                                                           "load": [num(x, 0) for x in load[:3]],
                                                           "procs": load[3] if len(load) > 3 else "?"},
            "cpu": {"total": usage.get("cpu", 0.0),
                    "cores": [usage[k] for k in sorted((k for k in usage if k != "cpu"), key=lambda k: int(k[3:]))],
                    "freq_mhz": sum(freqs) / len(freqs) if freqs else None, "freq_max": max(freqs) if freqs else None,
                    "temp": cpu_temp},
            "memory": memory,
            "gpus": slow["gpus"], "gpu_procs": slow["gpu_procs"],
            "network": interfaces, "addresses": slow["addresses"],
            "disks": disks, "filesystems": filesystems(),
            "temps": [{k: v for k, v in t.items() if k != "device"} for t in temps],
            "slow_updated": slow["updated"],
        }


class ProcessSampler:
    def __init__(self):
        self.prev: dict[int, int] = {}
        self.prev_time = None

    def sample(self) -> list[dict]:
        now = time.monotonic()
        procs = read_processes()
        dt = now - self.prev_time if self.prev_time else None
        out = []
        for pid, p in procs.items():
            prev = self.prev.get(pid)
            cpu = 100 * (p["ticks"] - prev) / CLK_TCK / dt if prev is not None and dt else 0.0
            out.append({**p, "cpu": max(0.0, cpu)})
        self.prev = {pid: p["ticks"] for pid, p in procs.items()}
        self.prev_time = now
        return out
