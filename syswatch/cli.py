"""syswatch command line."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .collect import Collector
from .ui import HEALTH, duration, rate, size


def summary(s: dict) -> str:
    sysi, c, m = s["system"], s["cpu"], s["memory"]
    out = [f"{sysi['hostname']} · {sysi['os']} · {sysi['kernel']} · up {duration(sysi['uptime'])} · "
           f"load {' '.join(f'{x:.2f}' for x in sysi['load'])}",
           f"CPU   {c['total']:5.1f}%  {sysi['cpu_count']} threads"
           + (f" · {c['freq_mhz'] / 1000:.2f} GHz" if c["freq_mhz"] else "")
           + (f" · {c['temp']:.0f}°C" if c["temp"] is not None else ""),
           f"RAM   {size(m['used'])} / {size(m['total'])} ({100 * m['used'] / (m['total'] or 1):.0f}%)"
           + (f" · swap {size(m['swap_used'])} / {size(m['swap_total'])}" if m["swap_total"] else "")]
    for g in s["gpus"]:
        line = f"GPU   {g['name']}"
        if g.get("util") is not None:
            line += f" · {g['util']:.0f}%"
        if g.get("vram_total"):
            line += f" · VRAM {size(g['vram_used'])} / {size(g['vram_total'])}"
        if g.get("temp") is not None:
            line += f" · {g['temp']:.0f}°C"
        if g.get("note"):
            line += f" ({g['note']})"
        out.append(line)
    for d in s["disks"]:
        out.append(f"Disk  {d['name']:8} {d['model'] or '-':22} {d['kind']:6} {size(d['size']):>10}  "
                   f"{HEALTH[d['health']][0]:10} {'; '.join(d['health_reasons'])}")
    for f in s["filesystems"]:
        pct = 100 * f["used"] / f["total"] if f["total"] else 0
        out.append(f"FS    {f['mount']:20} {pct:5.1f}% used · {size(f['free'])} free of {size(f['total'])} ({f['fstype']}, {f['disk']})")
    for n in s["network"]:
        if n["name"] == "lo":
            continue
        addrs = ", ".join(s["addresses"].get(n["name"], []))
        out.append(f"Net   {n['name']:8} {n['state']:5} ↓ {rate(n['rx_rate'])} ↑ {rate(n['tx_rate'])} {addrs}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="syswatch", description="Terminal system monitor: CPU, memory, GPU/VRAM, "
                                                             "disks with usage and health, network.")
    p.add_argument("--version", action="version", version=f"syswatch {__version__}")
    p.add_argument("-i", "--interval", type=float, default=1.0, help="refresh seconds (default 1)")
    p.add_argument("--once", action="store_true", help="print a text summary and exit")
    p.add_argument("--json", action="store_true", help="print one snapshot as JSON and exit")
    p.add_argument("--alerts", action="store_true",
                   help="check alert rules once, print them; exit 0 = none, 1 = warnings, 2 = critical")
    args = p.parse_args(argv)
    if args.alerts:
        from .alerts import AlertEngine
        c = Collector(slow=False)
        c.collect_slow()
        c.sample()
        time.sleep(1)
        s = c.sample()
        engine = AlertEngine(persist=False, immediate=True)   # reads saved state, never writes it
        engine.evaluate(s)
        active = engine.sorted_active()
        for a in active:
            print(f"{a.level.upper():5} {a.title}" + (f" - {a.detail}" if a.detail else "")
                  + ("  (acknowledged)" if a.acked else ""))
        if not active:
            print("OK    no alerts")
        worst = engine.worst()
        return 2 if worst == "crit" else 1 if worst == "warn" else 0
    if args.once or args.json or not sys.stdout.isatty():
        c = Collector(slow=False)
        c.collect_slow()
        c.sample()
        time.sleep(1)
        s = c.sample()
        print(json.dumps(s, indent=2, default=str) if args.json else summary(s))
        return 0
    from . import ui
    return ui.run(max(0.5, args.interval))
