"""Curses dashboard for syswatch."""

from __future__ import annotations

import curses
import locale
import os
import time

from kutil.fmt import bar, human_size as size, text_width as tw, truncate as trunc
from kutil.tui import Theme, pad, put

from .alerts import LEVELS, AlertEngine
from .collect import Collector, ProcessSampler

TABS = ["Overview", "Disks", "Network", "Processes", "Alerts"]
ALERT_ICON = {"crit": ("✗", "red"), "warn": ("!", "yellow"), "info": ("i", "cyan")}
SPARK = " ▁▂▃▄▅▆▇█"

HELP = """\
  1-5  Tab      switch view: Overview, Disks, Network, Processes, Alerts
  Disks view:   ↑↓ select a drive · Enter full drive report (SMART attributes, wear, self-tests, errors)
  ↑ ↓  PgUp PgDn  scroll when the view is taller than the window (Shift+↑↓ or K J: 5 rows)
  + -           refresh faster / slower (0.5 - 10 s)
  Space         pause / resume
  a             show all network interfaces (incl. loopback and virtual)
  r             re-check slow sources now (disk health, GPU processes, addresses)
  c m           Processes: sort by CPU / memory
  Alerts view:  ↑↓ select · a acknowledge/un-acknowledge · A acknowledge all · x clear history
                b toggle the terminal bell for new alerts
  q             quit"""


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def rate(n) -> str:
    return size(n) + "/s"


def short_rate(n) -> str:
    """Compact rate for tables: 0, 512B, 14.7K, 3.2M, 1.1G (per second)."""
    n = float(n or 0)
    if n < 1:
        return "0"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}/s" if unit == "B" else f"{n:.1f}{unit}/s"
        n /= 1024
    return "?"


def duration(seconds: float) -> str:
    s = int(seconds or 0)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def sparkline(values, width: int, top: float | None = None) -> str:
    vals = list(values)[-width:] if width > 0 else []
    if not vals:
        return " " * width
    top = top or max(vals) or 1
    out = "".join(SPARK[min(8, int(round(v / top * 8)))] if v > 0 else SPARK[0] for v in vals)
    return out.rjust(width)


def level_style(pct: float) -> str:
    return "red" if pct >= 90 else "yellow" if pct >= 70 else "green"


# --------------------------------------------------------------------------
# Curses plumbing
# --------------------------------------------------------------------------

class Line(list):
    """A display line: list of (text, style-name-tuple) segments."""

    def add(self, text: str, *styles: str) -> "Line":
        self.append((text, styles))
        return self

    def width(self) -> int:
        return sum(tw(t) for t, _ in self)


# --------------------------------------------------------------------------
# Panels (pure: data -> lines)
# --------------------------------------------------------------------------

def meter(label: str, frac: float, width: int, suffix: str, style: str | None = None) -> Line:
    bw = max(5, width - len(label) - tw(suffix) - 2)
    return Line().add(label, "bold").add(bar(frac, bw), style or level_style(frac * 100)).add(" " + suffix)


def cpu_panel(s: dict, hist, w: int) -> list[Line]:
    c, sysinfo = s["cpu"], s["system"]
    model = sysinfo["cpu_model"].replace("(R)", "").replace("(TM)", "").replace(" CPU", "")
    info = [f"{sysinfo['cpu_count']} threads"]
    if c["freq_mhz"]:
        info.append(f"{c['freq_mhz'] / 1000:.2f} GHz")
    lines = [Line().add(trunc(model, w - 30), "bold").add("  " + " · ".join(info), "dim")]
    if c["temp"] is not None:
        lines[0].add(f" · {c['temp']:.0f}°C", "red" if c["temp"] >= 85 else "yellow" if c["temp"] >= 70 else "dim")
    load = " ".join(f"{x:.2f}" for x in sysinfo["load"])
    lines.append(meter("Total ", c["total"] / 100, w, f"{c['total']:5.1f}%  load {load}"))
    cols = 4 if w >= 96 else 2 if w >= 40 else 1
    cell = w // cols
    row = Line()
    for i, pct in enumerate(c["cores"]):
        label = f"{i:>2} "
        bw = max(3, cell - len(label) - 6)
        row.add(label, "dim").add(bar(pct / 100, bw), level_style(pct)).add(f"{pct:4.0f}% ")
        if (i + 1) % cols == 0:
            lines.append(row)
            row = Line()
    if row:
        lines.append(row)
    lines.append(Line().add("hist  ", "dim").add(sparkline(hist.get("cpu", []), w - 6, 100), "cyan"))
    return lines


def memory_panel(s: dict, hist, w: int) -> list[Line]:
    m = s["memory"]
    total = m["total"] or 1
    lines = [meter("RAM  ", m["used"] / total, w, f"{size(m['used'])} / {size(m['total'])}  {100 * m['used'] / total:3.0f}%"),
             Line().add(f"     available {size(m['available'])} · cache {size(m['cached'])} · buffers {size(m['buffers'])}",
                        "dim")]
    if m["swap_total"]:
        lines.append(meter("Swap ", m["swap_used"] / m["swap_total"], w,
                           f"{size(m['swap_used'])} / {size(m['swap_total'])}"))
    else:
        lines.append(Line().add("Swap ", "bold").add("none", "dim"))
    lines.append(Line().add("hist ", "dim").add(sparkline(hist.get("mem", []), w - 5, 100), "magenta"))
    return lines


def gpu_panel(s: dict, w: int) -> list[Line]:
    lines = []
    if not s["gpus"]:
        return [Line().add("No GPU detected", "dim")]
    for g in s["gpus"]:
        head = Line().add(trunc(g["name"], w - 2), "bold")
        lines.append(head)
        bits = []
        if g.get("driver"):
            bits.append(f"driver {g['driver']}")
        if g.get("pstate"):
            bits.append(g["pstate"])
        if g.get("temp") is not None:
            bits.append(f"{g['temp']:.0f}°C")
        if g.get("fan") is not None:
            bits.append(f"fan {g['fan']:.0f}%")
        if g.get("power") is not None:
            bits.append(f"{g['power']:.0f}" + (f"/{g['power_limit']:.0f}" if g.get("power_limit") else "") + " W")
        if bits:
            lines.append(Line().add(" · ".join(bits), "dim"))
        if g.get("util") is not None:
            lines.append(meter("GPU  ", g["util"] / 100, w, f"{g['util']:3.0f}%"))
        if g.get("vram_total"):
            frac = g["vram_used"] / g["vram_total"]
            lines.append(meter("VRAM ", frac, w, f"{size(g['vram_used'])} / {size(g['vram_total'])}"))
        if g.get("clock"):
            lines.append(Line().add(f"     core {g['clock']:.0f} MHz · memory {g.get('mem_clock') or 0:.0f} MHz", "dim"))
        if g.get("note"):
            lines.append(Line().add(g["note"], "yellow"))
    for p in s.get("gpu_procs", [])[:4]:
        lines.append(Line().add(f"  {p['pid']:>7} ", "dim").add(pad(p["name"], w - 22)).add(f"{size(p['vram']):>11}"))
    return lines


def visible_ifaces(s: dict, show_all: bool) -> list[dict]:
    out = []
    for n in s["network"]:
        if not show_all and (n["name"] == "lo" or (n["virtual"] and n["state"] != "up" and not n["rx_rate"])):
            continue
        out.append(n)
    return out


def network_panel(s: dict, hist, w: int, show_all: bool, detail: bool = False) -> list[Line]:
    ifaces = visible_ifaces(s, show_all)
    rx = sum(n["rx_rate"] for n in s["network"] if n["name"] != "lo")
    tx = sum(n["tx_rate"] for n in s["network"] if n["name"] != "lo")
    lines = [Line().add("All  ", "bold").add(f"↓ {rate(rx):>12}", "cyan").add(f"   ↑ {rate(tx):>12}", "green")]
    half = max(10, (w - 4) // 2)
    for n in ifaces:
        state_style = "green" if n["state"] == "up" else "dim" if n["state"] == "unknown" else "red"
        head = Line().add(n["name"], "bold").add(f"  {n['state']}", state_style)
        if n["speed"]:
            head.add(f" · {n['speed'] / 1000:g} Gb/s" if n["speed"] >= 1000 else f" · {n['speed']:.0f} Mb/s", "dim")
        if n["wireless"]:
            head.add(" · wifi", "dim")
        addrs = s.get("addresses", {}).get(n["name"], [])
        if addrs:
            head.add("  " + ", ".join(addrs[:2 if not detail else 6]), "dim")
        lines.append(head)
        spark_w = max(4, half - 17)
        lines.append(Line().add(f" ↓ {rate(n['rx_rate']):>12} ", "cyan").add(sparkline(hist.get(f"rx:{n['name']}", []), spark_w), "cyan")
                     .add(f"  ↑ {rate(n['tx_rate']):>12} ", "green").add(sparkline(hist.get(f"tx:{n['name']}", []), spark_w), "green"))
        errs = n["rx_errs"] + n["tx_errs"]
        drops = n["rx_drop"] + n["tx_drop"]
        lines.append(Line().add(f"   total ↓ {size(n['rx'])} ↑ {size(n['tx'])}", "dim")
                     .add(f" · errors {errs}", "red" if errs else "dim").add(f" · drops {drops}", "yellow" if drops else "dim"))
        if detail:
            lines.append(Line().add(f"   packets ↓ {n['rx_packets']:,} ↑ {n['tx_packets']:,} · "
                                    f"rx errs/drops {n['rx_errs']}/{n['rx_drop']} · tx errs/drops {n['tx_errs']}/{n['tx_drop']}",
                                    "dim"))
            lines.append(Line())
    return lines


HEALTH = {"ok": ("✓ ok", "green"), "warn": ("! warning", "yellow"), "fail": ("✗ FAILING", "red")}


def disks_panel(s: dict, w: int) -> list[Line]:
    lines = []
    cols = [("Disk", 7, "<"), ("Model", 0, "<"), ("Type", 7, "<"), ("Size", 9, ">"), ("Temp", 5, ">"),
            ("Health", 9, "<"), ("Read", 8, ">"), ("Write", 8, ">"), ("Busy", 4, ">")]
    for drop in ("Temp", "Busy", "Model"):           # narrow panels lose these first
        fixed = sum(c[1] for c in cols) + len(cols)
        if w - fixed >= 10 or drop not in [c[0] for c in cols]:
            break
        cols = [c for c in cols if c[0] != drop]
    model_w = max(6, w - sum(c[1] for c in cols) - len(cols))

    def row(values, styles=None):
        line = Line()
        for (name, width, align), v, st in zip(cols, values, styles or [()] * len(values)):
            width = width or model_w
            line.add(pad(v, width, align) + " ", *st)
        return line
    lines.append(row([c[0] for c in cols], [("bold",)] * len(cols)))
    for d in s["disks"]:
        label, style = HEALTH[d["health"]]
        vals = {"Disk": d["name"], "Model": d["model"] or "-", "Type": d["kind"], "Size": size(d["size"]),
                "Temp": f"{d['temp']:.0f}°C" if d["temp"] is not None else "-", "Health": label,
                "Read": short_rate(d["read_rate"]), "Write": short_rate(d["write_rate"]), "Busy": f"{d['busy']:.0f}%"}
        styl = {"Health": (style, "bold"), "Disk": ("bold",), "Read": ("cyan",) if d["read_rate"] else ("dim",),
                "Write": ("magenta",) if d["write_rate"] else ("dim",),
                "Busy": (level_style(d["busy"]),) if d["busy"] >= 70 else ("dim",)}
        lines.append(row([vals[c[0]] for c in cols], [styl.get(c[0], ()) for c in cols]))
    lines.append(Line())
    mw = max(10, min(28, w // 4))
    bw = max(6, w - mw - 6 - 7 - 6 - 11 - 11 - 6)
    lines.append(Line().add(pad("Mount", mw) + " ", "bold").add(pad("Disk", 6) + " ", "bold").add(pad("FS", 6) + " ", "bold")
                 .add(pad("Used", bw + 6) + " ", "bold").add(pad("Free", 10, ">") + " ", "bold").add(pad("Size", 10, ">"), "bold"))
    for f in s["filesystems"]:
        frac = f["used"] / f["total"] if f["total"] else 0
        free_style = "red" if f["free"] < 5 * 1024 ** 3 or frac >= 0.95 else ""
        lines.append(Line().add(pad(f["mount"], mw) + " ").add(pad(f["disk"], 6) + " ", "dim")
                     .add(pad(f["fstype"], 6) + " ", "dim").add(bar(frac, bw), level_style(frac * 100))
                     .add(f"{frac * 100:5.0f}% ").add(pad(size(f["free"]), 10, ">") + " ", free_style)
                     .add(pad(size(f["total"]), 10, ">"), "dim"))
    return lines


def disk_detail_lines(s: dict, hist, w: int, selected: int | None = None, marks: list | None = None) -> list[Line]:
    lines = []
    for n, d in enumerate(s["disks"]):
        label, style = HEALTH[d["health"]]
        if marks is not None:
            marks.append(len(lines))
        sel = n == selected
        lines.append(Line().add("▶ " if sel else "  ", "yellow", "bold").add(f"{d['name']}", "bold", "cyan", *(("reverse",) if sel else ()))
                     .add(f"  {d['model'] or '?'}", "bold")
                     .add(f"  {d['kind']} · {d['transport'] or '?'} · {size(d['size'])}"
                          + (f" · serial {d['serial']}" if d.get("serial") else ""), "dim"))
        lines.append(Line().add("  Health  ", "dim").add(label, style, "bold").add("  " + "; ".join(
            r for r in d["health_reasons"] if "informational" not in r) + ("   ⏎ full report" if sel else "")))
        smart = d.get("smart") or {}
        if smart.get("available"):
            bits = [f"source {smart['source']}"]
            if smart.get("power_on_hours") is not None:
                bits.append(f"power-on {smart['power_on_hours']:.0f} h")
            if smart.get("bad_sectors", -1) >= 0:
                bits.append(f"bad/pending sectors {smart['bad_sectors']}")
            if smart.get("percent_used") is not None:
                bits.append(f"wear {smart['percent_used']:.0f}%")
            if smart.get("findings") is not None:
                bits.append(f"{len(smart['findings'])} attribute(s) need attention" if smart["findings"]
                            else "all key attributes clean")
            lines.append(Line().add("  SMART   ", "dim").add(" · ".join(bits)))
        else:
            lines.append(Line().add("  SMART   ", "dim").add("not available without root - run "
                                                         "~/scripts/syswatch-enable-health.sh once to enable", "yellow"))
        k = d.get("kernel") or {}
        lines.append(Line().add("  Kernel  ", "dim").add(f"state {k.get('state') or '?'} · commands {k.get('iodone_cnt', '?'):,} · "
                                                        f"errors {k.get('ioerr_cnt', '?')} · timeouts {k.get('iotmo_cnt', '?')}"
                                                        if isinstance(k.get("iodone_cnt"), int) else
                                                        f"state {k.get('state') or '?'}"))
        temp = f"{d['temp']:.0f}°C" if d["temp"] is not None else "unknown (needs the drivetemp module or SMART)"
        lines.append(Line().add("  Temp    ", "dim").add(temp))
        spark_w = max(10, w - 60)
        lines.append(Line().add("  I/O     ", "dim").add(f"read {rate(d['read_rate']):>12}  write {rate(d['write_rate']):>12}  ")
                     .add(sparkline(hist.get(f"io:{d['name']}", []), spark_w), "cyan"))
        for f in [f for f in s["filesystems"] if f["disk"] == d["name"]]:
            frac = f["used"] / f["total"] if f["total"] else 0
            lines.append(Line().add("  Mount   ", "dim").add(pad(f["mount"], 20) + " ")
                         .add(bar(frac, 20), level_style(frac * 100))
                         .add(f" {frac * 100:3.0f}%  {size(f['free'])} free of {size(f['total'])} · {f['fstype']} · {f['device']}", "dim"))
        for j in d.get("journal") or []:
            lines.append(Line().add("  Log     ", "dim").add(j, "red"))
        lines.append(Line())
    temps = [t for t in s["temps"]]
    if temps:
        lines.append(Line().add("Sensors", "bold"))
        for t in temps:
            st = "red" if t["crit"] and t["temp"] >= t["crit"] - 5 else "yellow" if t["max"] and t["temp"] >= t["max"] else "dim"
            lines.append(Line().add(f"  {pad(t['chip'], 12)} {pad(t['label'], 18)} ").add(f"{t['temp']:5.1f}°C", st))
    return lines


def _hours(h) -> str:
    if h is None:
        return "-"
    h = int(h)
    return f"{h:,} h (≈ {h / 8766:.1f} years)" if h >= 8766 else f"{h:,} h (≈ {h / 24:.0f} days)"


def _attr(data: dict, *ids):
    for a in (data.get("ata_smart_attributes") or {}).get("table", []):
        if a["id"] in ids:
            return a
    return None


def _devstat(data: dict, name: str):
    for page in (data.get("ata_device_statistics") or {}).get("pages", []):
        for t in page.get("table", []):
            if t.get("name") == name and t.get("value") is not None:
                return t["value"]
    return None


def report_lines(rep: dict, d: dict, filesystems: list, w: int) -> list[Line]:
    """Full detail for the drive popup."""
    data = rep.get("smart") or {}
    L: list[Line] = []

    def section(title):
        L.append(Line())
        L.append(Line().add(title, "bold", "cyan"))

    def kv(key, value, *styles):
        if value not in (None, ""):
            L.append(Line().add("  " + pad(key, 24), "dim").add(str(value), *styles))

    label, style = HEALTH[d["health"]]
    L.append(Line().add(label, style, "bold").add("  " + "; ".join(r for r in d["health_reasons"]
                                                                     if "informational" not in r)))
    status = data.get("smart_status")
    if status is not None:
        passed = status.get("passed", True)
        L.append(Line().add("SMART self-assessment: ", "dim").add("PASSED" if passed else "FAILED",
                                                                   "green" if passed else "red", "bold")
                 .add("  (the drive's own verdict; it only fails when a threshold is crossed)", "dim"))
    elif not rep.get("basic_available"):
        L.append(Line().add("SMART data needs root: run  sudo bash ~/scripts/syswatch-enable-health.sh", "yellow"))
    else:
        L.append(Line().add("smartctl returned no SMART data for this device", "yellow"))

    if rep.get("findings"):
        section("Needs attention")
        for f in rep["findings"]:
            L.append(Line().add("  ● ", "red" if f["level"] == "fail" else "yellow").add(f["text"], "bold")
                     .add(f"  {f.get('why', '')}", "dim"))
        if any(f.get("id") in (5, 183, 187, 197, 198) for f in rep["findings"]):
            L.append(Line().add("    Growing numbers here mean the media is wearing out: keep backups and plan a replacement.",
                                "yellow"))

    section("Drive")
    kv("Model family", data.get("model_family"))
    kv("Model", data.get("model_name") or d.get("model"))
    kv("Serial", data.get("serial_number") or d.get("serial"))
    kv("Firmware", data.get("firmware_version"))
    cap = (data.get("user_capacity") or {}).get("bytes") or data.get("nvme_total_capacity") or d.get("size")
    kv("Capacity", f"{size(cap)} ({cap:,} bytes)" if cap else None)
    if data.get("logical_block_size"):
        kv("Sector size", f"{data['logical_block_size']} B logical / {data.get('physical_block_size', '?')} B physical")
    rr = data.get("rotation_rate")
    kv("Type", "solid state (no rotation)" if rr == 0 else f"{rr} rpm hard disk" if rr else d.get("kind"))
    kv("Form factor", (data.get("form_factor") or {}).get("name"))
    sata = (data.get("sata_version") or {}).get("string")
    speed = data.get("interface_speed") or {}
    cur, mx = (speed.get("current") or {}).get("string"), (speed.get("max") or {}).get("string")
    if sata or cur:
        slow = cur and mx and cur != mx
        kv("Interface", f"{sata or d.get('transport', '')} · link {cur or '?'}" + (f" of max {mx}" if mx else "")
           + ("  ← running below max speed (port/cable/controller limit)" if slow else ""), "yellow" if slow else "")
    kv("Standard", (data.get("ata_version") or {}).get("string"))
    trim = data.get("trim")
    if trim:
        kv("TRIM", "supported" + (", deterministic" if trim.get("deterministic") else "")
           + (", zeroes trimmed data" if trim.get("zeroed") else "") if trim.get("supported") else "not supported")
    if data.get("in_smartctl_database") is not None:
        kv("Known to smartctl", "yes" if data["in_smartctl_database"] else
           "no (vendor attributes show as Unknown_Attribute)")

    section("Usage & wear")
    kv("Powered on", _hours((data.get("power_on_time") or {}).get("hours")))
    kv("Power cycles", f"{data['power_cycle_count']:,}" if data.get("power_cycle_count") is not None else None)
    a = _attr(data, 4)
    kv("Start/stop count", f"{a['raw']['value']:,}" if a else None)
    a = _attr(data, 193, 225)
    if a:
        kv("Head load cycles", f"{a['raw']['value']:,}  (desktop drives are typically rated for 300,000)")
    a = _attr(data, 192)
    kv("Emergency retracts", f"{a['raw']['value']:,}  (power lost while spinning)" if a else None)
    lbs = data.get("logical_block_size") or 512
    written = _devstat(data, "Logical Sectors Written")
    read = _devstat(data, "Logical Sectors Read")
    if written is not None:
        kv("Data written", size(written * lbs))
        kv("Data read", size((read or 0) * lbs))
    else:
        for aid, what in ((241, "Written"), (242, "Read")):
            a = _attr(data, aid)
            if a:
                kv(f"{what} (attr {aid})", f"{a['raw']['value']:,}  (vendor-specific units)")
    used = _devstat(data, "Percentage Used Endurance Indicator")
    if used is not None:
        kv("Endurance used", f"{used}%", "red" if used >= 90 else "yellow" if used >= 70 else "green")
    for a in (data.get("ata_smart_attributes") or {}).get("table", []):
        if a["id"] in (169, 173, 177, 202, 231, 233) and not a["name"].startswith("Unknown"):
            kv(a["name"].replace("_", " "), f"normalized {a['value']} (100 = new) · raw {a['raw']['string']}")
    temp = data.get("temperature") or {}
    sct = (data.get("ata_sct_status") or {}).get("temperature") or {}
    if temp.get("current") is not None:
        extra = ""
        if sct.get("lifetime_min") is not None:
            extra = f"  (lifetime {sct['lifetime_min']}–{sct.get('lifetime_max')}°C, this power-on " \
                    f"{sct.get('power_cycle_min')}–{sct.get('power_cycle_max')}°C)"
        else:
            a = _attr(data, 194, 190)
            if a and "Min/Max" in a["raw"].get("string", ""):
                extra = "  (" + a["raw"]["string"].split("(", 1)[1].rstrip(")") + ")"
        kv("Temperature", f"{temp['current']}°C{extra}")
    nv = data.get("nvme_smart_health_information_log") or {}
    if nv:
        kv("Endurance used", f"{nv.get('percentage_used', '?')}%")
        kv("Spare blocks", f"{nv.get('available_spare', '?')}% (threshold {nv.get('available_spare_threshold', '?')}%)")
        kv("Data written", size((nv.get("data_units_written") or 0) * 512000))
        kv("Data read", size((nv.get("data_units_read") or 0) * 512000))
        kv("Unsafe shutdowns", nv.get("unsafe_shutdowns"))
        kv("Media errors", nv.get("media_errors"), "red" if nv.get("media_errors") else "")
        kv("Error log entries", nv.get("num_err_log_entries"))
        kv("Time above warn temp", f"{nv.get('warning_temp_time', 0)} min")

    table = (data.get("ata_smart_attributes") or {}).get("table", [])
    if table:
        section("SMART attributes")
        nw = max(16, min(30, w - 50))
        L.append(Line().add("  " + pad("ID", 4, ">") + "  " + pad("Name", nw) + pad("Value", 6, ">") + pad("Worst", 6, ">")
                            + pad("Thresh", 7, ">") + "  " + pad("Raw", 18) + "Type", "bold"))
        for a in table:
            raw = a["raw"].get("string", str(a["raw"].get("value")))
            st = ()
            if a.get("when_failed"):
                st = ("red", "bold")
            elif a["id"] in (5, 10, 183, 184, 187, 188, 196, 197, 198, 199) and a["raw"].get("value"):
                st = ("yellow",)
            prefail = (a.get("flags") or {}).get("prefailure")
            L.append(Line().add("  " + pad(str(a["id"]), 4, ">") + "  ", "dim").add(pad(a["name"], nw), *st)
                     .add(pad(str(a.get("value")), 6, ">") + pad(str(a.get("worst")), 6, ">")
                          + pad(str(a.get("thresh")), 7, ">") + "  ").add(pad(raw, 18), *st)
                     .add("pre-fail" if prefail else "old-age", "dim")
                     .add(f"  FAILED {a['when_failed']}" if a.get("when_failed") else "", "red", "bold"))

    if rep.get("mode") == "full":
        tests = ((data.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table") \
            or (data.get("nvme_self_test_log") or {}).get("table") or []
        section("Self-tests")
        if not tests:
            L.append(Line().add("  no self-tests recorded (run one with: sudo smartctl -t short /dev/" + rep["name"] + ")",
                                "dim"))
        for t in tests[:8]:
            status = (t.get("status") or {}).get("string") or (t.get("self_test_result") or {}).get("string", "?")
            ok = "without error" in status.lower() or "completed" == status.lower()
            L.append(Line().add("  " + pad((t.get("type") or t.get("self_test_code") or {}).get("string", "?"), 20))
                     .add(pad(status, 44), "green" if ok else "red").add(f"at {t.get('lifetime_hours', t.get('power_on_hours', '?'))} h", "dim"))
        errlog = (data.get("ata_smart_error_log") or {})
        errs = (errlog.get("extended") or errlog.get("summary") or {})
        section("Drive error log")
        count = errs.get("count", 0)
        L.append(Line().add(f"  {count} error(s) logged by the drive", "red" if count else "green"))
        for e in (errs.get("table") or [])[:5]:
            L.append(Line().add(f"  #{e.get('error_number', '?')} at {e.get('lifetime_hours', '?')} h: "
                                f"{e.get('error_description', '?')}", "dim"))
    elif rep.get("basic_available") and not rep.get("full_available"):
        L.append(Line())
        L.append(Line().add("Self-test history, the drive's own error log and temperature history need the updated "
                            "enable script: sudo bash ~/scripts/syswatch-enable-health.sh", "yellow"))

    section("Kernel")
    k = rep.get("kernel") or {}
    kv("Device state", k.get("state"), "green" if k.get("state") == "running" else "red")
    if isinstance(k.get("iodone_cnt"), int):
        kv("Commands since boot", f"{k['iodone_cnt']:,}")
        kv("Command errors", f"{k.get('ioerr_cnt', 0):,}  (includes SMART queries, which the kernel counts as errors)")
        kv("Timeouts", f"{k.get('iotmo_cnt', 0):,}", "yellow" if k.get("iotmo_cnt") else "")
    journal = rep.get("journal") or []
    if journal:
        L.append(Line().add(f"  {len(journal)} disk error message(s) in the kernel log (7 days):", "red"))
        for j in journal[-8:]:
            L.append(Line().add("    " + j, "dim"))
    else:
        L.append(Line().add("  no disk errors in the kernel log for the last 7 days", "green"))

    mine = [f for f in filesystems if f["disk"] == d["name"]]
    if mine:
        section("Filesystems on this drive")
        for f in mine:
            frac = f["used"] / f["total"] if f["total"] else 0
            L.append(Line().add("  " + pad(f["mount"], 18) + " ").add(bar(frac, 16), level_style(frac * 100))
                     .add(f" {frac * 100:3.0f}%  {size(f['free'])} free of {size(f['total'])} · {f['fstype']} · {f['device']}",
                          "dim"))
    L.append(Line())
    src = {"full": "smartctl -x (full report)", "basic": "smartctl -H -A -i"}.get(rep.get("mode"), "kernel only")
    L.append(Line().add(f"Source: {src} · read {time.strftime('%H:%M:%S', time.localtime(rep.get('time', 0)))}", "dim"))
    return L


def ago(t: float, now: float | None = None) -> str:
    return duration(max(0, (now or time.time()) - t)) if t else "-"


def alert_line(a, w: int, now: float, selected: bool = False) -> Line:
    icon, style = ALERT_ICON[a.level]
    rev = ("reverse",) if selected else ()
    line = Line().add(f" {icon} ", style, "bold", *rev)
    title_style = ("dim",) if a.acked or a.resolved else ("bold",)
    when = f"resolved {ago(a.resolved, now)} ago" if a.resolved else (
        "event " + ago(a.first, now) + " ago" if a.event else f"for {ago(a.first, now)}")
    tail = f"  {when}" + ("  (acknowledged)" if a.acked else "")
    line.add(trunc(a.title, max(10, w - tw(tail) - 4)), *title_style, *rev).add(tail, "dim")
    return line


def alerts_widget_lines(engine, w: int, limit: int = 4) -> list[Line]:
    now = time.time()
    active = [a for a in engine.sorted_active() if not a.acked]
    c = engine.counts()
    if not active:
        acked = len(engine.active)
        return [Line().add("✓ No active alerts", "green", "bold")
                .add(f"  ({acked} acknowledged)" if acked else "", "dim")]
    lines = [Line().add(f"{c['crit']} critical", "red", "bold").add(" · ", "dim")
             .add(f"{c['warn']} warning", "yellow", "bold").add("   5 for details", "dim")]
    for a in active[:limit]:
        lines.append(alert_line(a, w, now))
        if a.detail:
            lines.append(Line().add("     " + trunc(a.detail, w - 6), "dim"))
    if len(active) > limit:
        lines.append(Line().add(f"   +{len(active) - limit} more", "dim"))
    return lines


def alerts_tab_lines(engine, w: int, selected: int, marks: list) -> tuple[list[Line], list]:
    """Active alerts then history; returns (lines, selectable alerts in order)."""
    now = time.time()
    active = engine.sorted_active()
    items = active + engine.history[:100]
    lines: list[Line] = [Line().add(f"Active ({len(active)})", "bold", "cyan")
                         .add("   a acknowledge · A all · x clear history · b bell "
                              + ("on" if getattr(engine, "bell", False) else "off"), "dim")]
    if not active:
        lines.append(Line().add("   ✓ nothing wrong right now", "green"))
    for i, a in enumerate(items):
        if i == len(active):
            lines.append(Line())
            lines.append(Line().add(f"History ({len(engine.history)})", "bold", "cyan"))
        marks.append(len(lines))
        sel = i == selected
        lines.append(alert_line(a, w, now, sel))
        if a.detail and (sel or not a.resolved):
            lines.append(Line().add("     " + trunc(a.detail, w - 6), "dim"))
        if sel:
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.first))
            extra = f"     {a.category} · started {started}"
            if a.resolved:
                extra += " · resolved " + time.strftime("%H:%M:%S", time.localtime(a.resolved))
            lines.append(Line().add(extra, "dim"))
    if not engine.history and active:
        lines.append(Line())
        lines.append(Line().add("History (0)", "bold", "cyan"))
    return lines, items


def process_lines(procs: list[dict], total_mem: int, w: int, sort: str, limit: int) -> list[Line]:
    key = (lambda p: -p["cpu"]) if sort == "cpu" else (lambda p: -p["rss"])
    name_w = max(10, w - 7 - 13 - 8 - 11 - 7 - 5)
    lines = [Line().add(pad("PID", 7, ">") + " ", "bold").add(pad("User", 12) + " ", "bold")
             .add(pad("CPU%" + (" ▼" if sort == "cpu" else ""), 7, ">") + " ", "bold")
             .add(pad("Memory" + (" ▼" if sort == "mem" else ""), 10, ">") + " ", "bold")
             .add(pad("Mem%", 6, ">") + " ", "bold").add(pad("S", 2) + " ", "bold").add("Name", "bold")]
    for p in sorted(procs, key=key)[:limit]:
        mem_pct = 100 * p["rss"] / total_mem if total_mem else 0
        lines.append(Line().add(pad(str(p["pid"]), 7, ">") + " ", "dim").add(pad(p["user"], 12) + " ")
                     .add(pad(f"{p['cpu']:.1f}", 7, ">") + " ", level_style(p["cpu"]) if p["cpu"] >= 50 else "")
                     .add(pad(size(p["rss"]), 10, ">") + " ").add(pad(f"{mem_pct:.1f}", 6, ">") + " ", "dim")
                     .add(pad(p["state"], 2) + " ", "dim").add(trunc(p["name"], name_w), "bold" if p["cpu"] >= 10 else ""))
    return lines


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class App:
    def __init__(self, scr, interval: float):
        self.scr = scr
        self.theme = Theme()
        self.collector = Collector()
        self.procs = ProcessSampler()
        self.interval = interval
        self.tab = 0
        self.scroll = [0] * len(TABS)
        self.paused = False
        self.show_all = False
        self.sort = "cpu"
        self.snap = None
        self.proc_list: list[dict] = []
        self.last = 0.0
        self.help = False
        self.disk_sel = 0
        self.alerts = AlertEngine()
        self.alerts.bell = False
        self.alert_sel = 0
        self.notice: tuple[str, str, float] | None = None

    def tick(self) -> None:
        self.collector.fast_gpu()
        self.snap = self.collector.sample()
        if self.snap.get("interval"):   # skip the first sample: rates aren't known yet
            new = self.alerts.evaluate(self.snap)
            if new:
                worst = max(new, key=lambda a: LEVELS[a.level])
                self.notice = (f"{ALERT_ICON[worst.level][0]} {worst.title}"
                               + (f"  (+{len(new) - 1} more)" if len(new) > 1 else ""),
                               ALERT_ICON[worst.level][1], time.monotonic() + 10)
                if self.alerts.bell:
                    curses.beep()
        if self.tab == 3:
            self.proc_list = self.procs.sample()
        self.last = time.monotonic()

    def run(self) -> int:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(100)
        self.tick()
        time.sleep(0.3)
        self.tick()
        try:
            while True:
                if not self.paused and time.monotonic() - self.last >= self.interval:
                    self.tick()
                self.draw()
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue
                if not self.handle(key):
                    return 0
        except KeyboardInterrupt:
            return 0
        finally:
            self.collector.stop()

    def handle(self, key) -> bool:
        if self.help:
            self.help = False
            return True
        if self.tab == 4:
            items = self.alerts.sorted_active() + self.alerts.history[:100]
            if key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k") and items:
                self.alert_sel = (self.alert_sel + (1 if key in (curses.KEY_DOWN, "j") else -1)) % len(items)
                self.follow_alert = True
                return True
            if key == "a" and items:
                a = items[min(self.alert_sel, len(items) - 1)]
                if not a.resolved:
                    self.alerts.ack(a.key, not a.acked)
                return True
            if key == "A":
                self.alerts.ack_all()
                return True
            if key == "x":
                self.alerts.clear_history()
                self.alert_sel = 0
                return True
            if key == "b":
                self.alerts.bell = not self.alerts.bell
                return True
        if self.tab == 1 and self.snap and self.snap["disks"]:
            n = len(self.snap["disks"])
            if key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k"):
                self.disk_sel = (self.disk_sel + (1 if key in (curses.KEY_DOWN, "j") else -1)) % n
                self.follow_disk = True
                return True
            if key in ("\n", "\r", curses.KEY_ENTER):
                self.disk_popup(self.snap["disks"][min(self.disk_sel, n - 1)])
                return True
        if key in ("q", "Q", "\x1b"):
            return False
        if key == "?":
            self.help = True
        elif key in ("1", "2", "3", "4", "5"):
            self.tab = int(key) - 1
            if self.tab == 3:
                self.procs.sample()
        elif key == "\t":
            self.tab = (self.tab + 1) % len(TABS)
        elif key == curses.KEY_BTAB:
            self.tab = (self.tab - 1) % len(TABS)
        elif key in ("+", "="):
            self.interval = max(0.5, self.interval / 2)
        elif key == "-":
            self.interval = min(10.0, self.interval * 2)
        elif key == " ":
            self.paused = not self.paused
        elif key == "a":
            self.show_all = not self.show_all
        elif key == "r":
            self.collector.refresh_slow()
        elif key in ("c", "m"):
            self.sort = "cpu" if key == "c" else "mem"
        elif key in (curses.KEY_SF, "J"):
            self.scroll[self.tab] += 5
        elif key in (curses.KEY_SR, "K"):
            self.scroll[self.tab] = max(0, self.scroll[self.tab] - 5)
        elif key in (curses.KEY_DOWN, "j"):
            self.scroll[self.tab] += 1
        elif key in (curses.KEY_UP, "k"):
            self.scroll[self.tab] = max(0, self.scroll[self.tab] - 1)
        elif key == curses.KEY_NPAGE:
            self.scroll[self.tab] += self.scr.getmaxyx()[0] - 4
        elif key == curses.KEY_PPAGE:
            self.scroll[self.tab] = max(0, self.scroll[self.tab] - (self.scr.getmaxyx()[0] - 4))
        elif key in (curses.KEY_HOME, "g"):
            self.scroll[self.tab] = 0
        return True

    # -- drawing ------------------------------------------------------------------

    def draw_line(self, y: int, x: int, line: Line, width: int, extra: int = 0) -> None:
        for text, styles in line:
            if x >= x + width:
                break
            put(self.scr, y, x, text, self.theme(*styles) | extra, width=max(0, width))
            width -= tw(text)
            x += tw(text)
            if width <= 0:
                break

    def panel(self, y: int, x: int, w: int, title: str, lines: list[Line], top: int, bottom: int) -> int:
        """Draw a boxed panel at virtual row y (scrolled); returns its height."""
        th = self.theme
        h = len(lines) + 2
        for i in range(h):
            vy = y + i - self.scroll[self.tab]
            if vy < top or vy >= bottom:
                continue
            if i == 0:
                put(self.scr, vy, x, "┌" + "─" * (w - 2) + "┐", th("dim"))
                put(self.scr, vy, x + 2, f" {title} ", th("bold", "cyan"))
            elif i == h - 1:
                put(self.scr, vy, x, "└" + "─" * (w - 2) + "┘", th("dim"))
            else:
                put(self.scr, vy, x, "│", th("dim"))
                put(self.scr, vy, x + w - 1, "│", th("dim"))
                self.draw_line(vy, x + 2, lines[i - 1], w - 4)
        return h

    def draw(self, refresh: bool = True) -> None:
        scr, th, s = self.scr, self.theme, self.snap
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 10 or w < 50:
            put(scr, 0, 0, "Window too small for syswatch (min 50×10)")
            scr.refresh()
            return
        sysi = s["system"]
        put(scr, 0, 0, " " * w, th("bar"))
        left = (f" syswatch │ {sysi['hostname']} · {sysi['os']} · {sysi['kernel']} · up {duration(sysi['uptime'])} · "
                f"load {' '.join(f'{x:.2f}' for x in sysi['load'])}")
        right = (" PAUSED " if self.paused else "") + time.strftime(" %H:%M:%S ")
        c = self.alerts.counts()
        badge = (f" ✗ {c['crit']} critical " if c["crit"] else "") + (f" ! {c['warn']} warning " if c["warn"] else "")
        put(scr, 0, 0, trunc(left, w - tw(right) - tw(badge) - 1), th("bar", "bold"))
        if badge:
            put(scr, 0, w - tw(right) - tw(badge), badge, th("red" if c["crit"] else "yellow", "reverse", "bold"))
        put(scr, 0, w - tw(right), right, th("bar"))
        x = 1
        for i, name in enumerate(TABS):
            text = f" {i + 1} {name} "
            if name == "Alerts" and (c["crit"] or c["warn"]):
                text = f" {i + 1} Alerts ({c['crit'] + c['warn']}) "
            style = th("reverse", "bold") if i == self.tab else (
                th("red" if c["crit"] else "yellow", "bold") if name == "Alerts" and (c["crit"] or c["warn"]) else th("dim"))
            put(scr, 1, x, text, style)
            x += tw(text) + 1
        put(scr, 1, w - 18, f"every {self.interval:g}s".rjust(16), th("dim"))
        top, bottom = 2, h - 1
        hist = self.collector.history
        content_h = 0
        if self.tab == 0:
            if w >= 110:
                lw = w // 2
                rw = w - lw
                y = top
                y += self.panel(y, 0, lw, "CPU", cpu_panel(s, hist, lw - 4), top, bottom)
                y += self.panel(y, 0, lw, "Memory", memory_panel(s, hist, lw - 4), top, bottom)
                y += self.panel(y, 0, lw, "GPU", gpu_panel(s, lw - 4), top, bottom)
                y2 = top
                y2 += self.panel(y2, lw, rw, "Alerts", alerts_widget_lines(self.alerts, rw - 4), top, bottom)
                y2 += self.panel(y2, lw, rw, "Network", network_panel(s, hist, rw - 4, self.show_all), top, bottom)
                y2 += self.panel(y2, lw, rw, "Disks & filesystems", disks_panel(s, rw - 4), top, bottom)
                content_h = max(y, y2) - top
            else:
                y = top
                for title, lines in (("Alerts", alerts_widget_lines(self.alerts, w - 4, 2)),
                                     ("CPU", cpu_panel(s, hist, w - 4)), ("Memory", memory_panel(s, hist, w - 4)),
                                     ("GPU", gpu_panel(s, w - 4)),
                                     ("Network", network_panel(s, hist, w - 4, self.show_all)),
                                     ("Disks & filesystems", disks_panel(s, w - 4))):
                    y += self.panel(y, 0, w, title, lines, top, bottom)
                content_h = y - top
        elif self.tab == 1:
            marks: list[int] = []
            self.disk_sel = min(self.disk_sel, max(0, len(s["disks"]) - 1))
            lines = disk_detail_lines(s, hist, w - 4, self.disk_sel, marks)
            if marks and getattr(self, "follow_disk", False):   # keep the selected drive's block in view
                first = marks[self.disk_sel] + 1
                last = (marks[self.disk_sel + 1] if self.disk_sel + 1 < len(marks) else len(lines)) + 1
                view = bottom - top
                if first < self.scroll[1]:
                    self.scroll[1] = first
                elif last > self.scroll[1] + view:
                    self.scroll[1] = max(0, min(first, last - view))
                self.follow_disk = False
            content_h = self.panel(top, 0, w, "Disks, health & sensors · ↑↓ select drive · ⏎ full report", lines,
                                   top, bottom)
        elif self.tab == 2:
            content_h = self.panel(top, 0, w, "Network" + (" (all interfaces)" if self.show_all else ""),
                                   network_panel(s, hist, w - 4, self.show_all, detail=True), top, bottom)
        elif self.tab == 4:
            marks: list[int] = []
            items_n = len(self.alerts.active) + min(100, len(self.alerts.history))
            self.alert_sel = min(self.alert_sel, max(0, items_n - 1))
            lines, _ = alerts_tab_lines(self.alerts, w - 4, self.alert_sel, marks)
            if marks and getattr(self, "follow_alert", False):
                pos = marks[min(self.alert_sel, len(marks) - 1)] + 1
                view = bottom - top
                if pos < self.scroll[4]:
                    self.scroll[4] = pos
                elif pos + 3 > self.scroll[4] + view:
                    self.scroll[4] = pos + 3 - view
                self.follow_alert = False
            content_h = self.panel(top, 0, w, "Alerts", lines, top, bottom)
        else:
            limit = max(5, h - 6)
            lines = process_lines(self.proc_list, s["memory"]["total"], w - 4, self.sort, limit)
            content_h = self.panel(top, 0, w, f"Processes ({len(self.proc_list)}) · c CPU · m memory", lines, top,
                                   bottom)
        max_scroll = max(0, content_h - (bottom - top))
        if self.scroll[self.tab] > max_scroll:
            self.scroll[self.tab] = max_scroll
        hint = " 1-5 views · ↑↓ scroll · +/- speed · space pause · a all interfaces · r recheck · ? help · q quit"
        if max_scroll:
            hint = f" ↓ more ({self.scroll[self.tab]}/{max_scroll}) ·" + hint
        if self.notice and self.notice[2] > time.monotonic():
            put(scr, h - 1, 0, pad(" New alert: " + self.notice[0] + "   (5 to view)", w), th(self.notice[1], "bold", "reverse"))
        else:
            put(scr, h - 1, 0, pad(hint, w), th("dim"))
        if self.help:
            lines = HELP.split("\n")
            bw, bh = min(w - 4, max(tw(l) for l in lines) + 6), len(lines) + 4
            y0, x0 = (h - bh) // 2, (w - bw) // 2
            for i in range(bh):
                put(scr, y0 + i, x0, " " * bw)
            put(scr, y0, x0, "┌" + "─" * (bw - 2) + "┐", th("cyan"))
            put(scr, y0, x0 + 2, " Keys ", th("bold", "cyan"))
            for i, l in enumerate(lines):
                put(scr, y0 + 2 + i, x0 + 2, l)
                put(scr, y0 + 1 + i, x0, "│", th("cyan"))
                put(scr, y0 + 1 + i, x0 + bw - 1, "│", th("cyan"))
            for i in range(len(lines), len(lines) + 2):
                put(scr, y0 + 1 + i, x0, "│", th("cyan"))
                put(scr, y0 + 1 + i, x0 + bw - 1, "│", th("cyan"))
            put(scr, y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘", th("cyan"))
        if refresh:
            scr.refresh()

    def disk_popup(self, disk: dict) -> None:
        """Scrollable full report for one drive; SMART is read in the background."""
        import threading
        from .collect import disk_report
        state = {"report": None, "loading": True}

        def load():
            state["loading"] = True
            state["report"] = disk_report(disk["name"])
            state["loading"] = False
        threading.Thread(target=load, daemon=True).start()
        top_line = 0
        th, scr = self.theme, self.scr
        while True:
            if not self.paused and time.monotonic() - self.last >= self.interval:
                self.tick()
            current = next((d for d in self.snap["disks"] if d["name"] == disk["name"]), disk)
            self.draw(refresh=False)
            h, w = scr.getmaxyx()
            bw, bh = min(w - 2, 118), h - 2
            y0, x0 = 1, max(0, (w - bw) // 2)
            for i in range(bh):
                put(scr, y0 + i, x0, " " * bw)
            title = f" /dev/{disk['name']} · {current.get('model') or '?'} · {current['kind']} · {size(current['size'])} "
            put(scr, y0, x0, "┌" + "─" * (bw - 2) + "┐", th("cyan"))
            put(scr, y0, x0 + 2, title, th("bold", "cyan"))
            for i in range(1, bh - 1):
                put(scr, y0 + i, x0, "│", th("cyan"))
                put(scr, y0 + i, x0 + bw - 1, "│", th("cyan"))
            put(scr, y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘", th("cyan"))
            body = bh - 3
            if state["report"] is None:
                put(scr, y0 + 2, x0 + 3, "Reading SMART data…", th("dim"))
                lines = []
            else:
                lines = report_lines(state["report"], current, self.snap["filesystems"], bw - 6)
                top_line = max(0, min(top_line, len(lines) - body))
                for i, line in enumerate(lines[top_line:top_line + body]):
                    self.draw_line(y0 + 1 + i, x0 + 3, line, bw - 6)
            more = f"{top_line + 1}-{min(len(lines), top_line + body)}/{len(lines)} · " if len(lines) > body else ""
            hint = f" {more}↑↓ scroll · Shift+↑↓ 5 · PgUp/PgDn · r re-read · Esc close "
            put(scr, y0 + bh - 2, x0 + 2, pad(("refreshing… " if state["loading"] and state["report"] else "") + hint,
                                             bw - 4), th("dim"))
            scr.refresh()
            try:
                key = scr.get_wch()
            except curses.error:
                continue
            page = max(1, body - 2)
            if key in ("q", "\x1b", "\n", "\r", curses.KEY_ENTER):
                return
            if key in (curses.KEY_DOWN, "j"):
                top_line += 1
            elif key in (curses.KEY_UP, "k"):
                top_line -= 1
            elif key in (curses.KEY_SF, "J"):
                top_line += 5
            elif key in (curses.KEY_SR, "K"):
                top_line -= 5
            elif key == curses.KEY_NPAGE:
                top_line += page
            elif key == curses.KEY_PPAGE:
                top_line -= page
            elif key in (curses.KEY_HOME, "g"):
                top_line = 0
            elif key in (curses.KEY_END, "G"):
                top_line = 10 ** 6
            elif key == "r" and not state["loading"]:
                threading.Thread(target=load, daemon=True).start()
            top_line = max(0, top_line)


def run(interval: float = 1.0) -> int:
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    return curses.wrapper(lambda scr: App(scr, interval).run())
