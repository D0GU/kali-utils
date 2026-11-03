"""Alert rules, lifecycle and persistence for syswatch.

Two kinds of alerts:
  condition  - true while something is wrong (CPU hot, disk nearly full, link down); resolves by itself.
               "Busy"-type rules must hold for `sustain` seconds and clear below a lower threshold (hysteresis).
  event      - something happened (SMART bad-sector count grew, new network errors); stays until acknowledged.

Acknowledging hides an alert from the counters/widget; if its details change (e.g. more bad sectors)
it becomes unacknowledged again. History, acknowledgements and last-seen SMART counters are kept in
~/.local/state/syswatch/alerts.json so they survive restarts.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from dataclasses import asdict, dataclass

LEVELS = {"crit": 2, "warn": 1, "info": 0}
GB = 1024 ** 3

DEFAULTS = {
    "cpu_percent": 90, "cpu_percent_seconds": 60,
    "cpu_temp_warn": 85, "cpu_temp_crit": 95,
    "load_per_core": 2.0, "load_seconds": 120,
    "mem_percent_warn": 90, "mem_percent_crit": 97,
    "swap_percent": 80,
    "fs_percent_warn": 90, "fs_percent_crit": 97, "fs_min_free_gb": 2,
    "hdd_temp_warn": 50, "hdd_temp_crit": 60, "ssd_temp_warn": 70, "ssd_temp_crit": 80,
    "gpu_temp_warn": 85, "gpu_temp_crit": 92, "vram_percent": 95,
    "net_errors": 10,
}


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "syswatch", "alerts.toml")


def state_path() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "syswatch", "alerts.json")


def load_thresholds(path: str | None = None) -> dict:
    th = dict(DEFAULTS)
    path = path or config_path()
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                th.update({k: v for k, v in tomllib.load(f).items() if k in DEFAULTS})
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return th


@dataclass
class Alert:
    key: str
    level: str
    category: str
    title: str
    detail: str = ""
    first: float = 0.0
    last: float = 0.0
    resolved: float | None = None
    acked: bool = False
    event: bool = False
    sig: str = ""            # what an acknowledgement refers to; a different sig shows the alert again


@dataclass
class Condition:
    key: str
    level: str | None       # None = below trigger but still above the clear threshold (keeps an alert active)
    category: str
    title: str
    detail: str = ""
    sustain: float = 0.0
    sig: str = ""            # defaults to the level: an acknowledged alert returns only if it escalates

    def signature(self) -> str:
        return self.sig or (self.level or "")


def _ident(disk: dict) -> str:
    return disk.get("serial") or disk.get("model") or disk["name"]


class AlertEngine:
    def __init__(self, thresholds: dict | None = None, path: str | None = None, persist: bool = True,
                 immediate: bool = False):
        self.th = thresholds or load_thresholds()
        self.path = path or state_path()
        self.persist = persist
        self.immediate = immediate          # ignore `sustain` (one-shot checks such as --alerts)
        self.active: dict[str, Alert] = {}
        self.history: list[Alert] = []
        self.acked: dict[str, str] = {}     # key -> signature at the time it was acknowledged
        self.smart_seen: dict[str, dict] = {}
        self.pending: dict[str, float] = {}
        self.net_base: dict[str, tuple[float, int]] = {}
        self.ifaces_up: set[str] = set()
        self._load()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.history = [Alert(**a) for a in data.get("history", [])][:200]
        self.acked = data.get("acked", {})
        self.smart_seen = data.get("smart", {})
        for a in data.get("events", []):   # unacknowledged events survive restarts
            al = Alert(**a)
            self.active[al.key] = al

    def save(self) -> None:
        if not self.persist:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {"history": [asdict(a) for a in self.history[:200]], "acked": self.acked, "smart": self.smart_seen,
                "events": [asdict(a) for a in self.active.values() if a.event]}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    # -- rules -------------------------------------------------------------------------

    def conditions(self, s: dict) -> list[Condition]:
        th, out = self.th, []

        def level_for(value, warn, crit=None):
            if crit is not None and value >= crit:
                return "crit"
            return "warn" if value >= warn else None

        cpu = s["cpu"]
        if cpu["total"] >= th["cpu_percent"]:
            out.append(Condition("cpu:usage", "warn", "cpu", f"CPU busy: {cpu['total']:.0f}%",
                                 f"above {th['cpu_percent']}% for {th['cpu_percent_seconds']} s",
                                 th["cpu_percent_seconds"]))
        elif cpu["total"] >= th["cpu_percent"] - 15:
            out.append(Condition("cpu:usage", None, "cpu", ""))
        if cpu.get("temp") is not None:
            lv = level_for(cpu["temp"], th["cpu_temp_warn"], th["cpu_temp_crit"])
            if lv or cpu["temp"] >= th["cpu_temp_warn"] - 5:
                out.append(Condition("cpu:temp", lv, "cpu", f"CPU hot: {cpu['temp']:.0f}°C",
                                     f"warn at {th['cpu_temp_warn']}°C, critical at {th['cpu_temp_crit']}°C", 10))
        cores = s["system"].get("cpu_count") or 1
        load1 = (s["system"].get("load") or [0])[0]
        if load1 >= th["load_per_core"] * cores:
            out.append(Condition("cpu:load", "warn", "cpu", f"High load: {load1:.1f} on {cores} threads",
                                 f"above {th['load_per_core']:g} per thread", th["load_seconds"]))
        elif load1 >= th["load_per_core"] * cores * 0.75:
            out.append(Condition("cpu:load", None, "cpu", ""))

        m = s["memory"]
        if m["total"]:
            pct = 100 * m["used"] / m["total"]
            lv = level_for(pct, th["mem_percent_warn"], th["mem_percent_crit"])
            if lv or pct >= th["mem_percent_warn"] - 5:
                out.append(Condition("mem:ram", lv, "memory", f"Memory {pct:.0f}% used",
                                     f"{m['available'] / GB:.1f} GiB available of {m['total'] / GB:.1f} GiB", 30))
        if m.get("swap_total"):
            pct = 100 * m["swap_used"] / m["swap_total"]
            if pct >= th["swap_percent"] or (pct >= th["swap_percent"] - 10 and "mem:swap" in self.active):
                out.append(Condition("mem:swap", "warn" if pct >= th["swap_percent"] else None, "memory",
                                     f"Swap {pct:.0f}% used", "the system is short of RAM", 30))

        for f in s["filesystems"]:
            if not f["total"]:
                continue
            pct = 100 * f["used"] / f["total"]
            low_free = f["total"] > 50 * GB and f["free"] < th["fs_min_free_gb"] * GB
            lv = "crit" if pct >= th["fs_percent_crit"] or low_free else "warn" if pct >= th["fs_percent_warn"] else None
            if lv or pct >= th["fs_percent_warn"] - 2:
                out.append(Condition(f"fs:{f['mount']}", lv, "fs", f"{f['mount']} {pct:.0f}% full",
                                     f"{f['free'] / GB:.1f} GiB free of {f['total'] / GB:.1f} GiB ({f['device']})"))

        for d in s["disks"]:
            ident = _ident(d)
            if d["health"] != "ok":
                reasons = "; ".join(r for r in d["health_reasons"] if "informational" not in r)
                level = "crit" if d["health"] == "fail" else "warn"
                out.append(Condition(f"disk:{ident}:health", level, "disk",
                                     f"{d['name']} ({d.get('model') or d['kind']}): "
                                     + ("FAILING" if d["health"] == "fail" else "health warning"), reasons,
                                     sig=f"{level}|{reasons}"))
            if d.get("temp") is not None:
                hdd = "HDD" in d["kind"]
                warn, crit = (th["hdd_temp_warn"], th["hdd_temp_crit"]) if hdd else (th["ssd_temp_warn"], th["ssd_temp_crit"])
                lv = level_for(d["temp"], warn, crit)
                if lv or d["temp"] >= warn - 3:
                    out.append(Condition(f"disk:{ident}:temp", lv, "disk", f"{d['name']} hot: {d['temp']:.0f}°C",
                                         f"warn at {warn}°C, critical at {crit}°C", 30))

        for n in s["network"]:
            if n["name"] == "lo" or n.get("virtual"):
                continue
            if n["state"] == "up":
                self.ifaces_up.add(n["name"])
            elif n["name"] in self.ifaces_up:
                out.append(Condition(f"net:{n['name']}:down", "warn", "net", f"{n['name']} link is {n['state']}",
                                     "the interface was up earlier in this session"))

        for i, g in enumerate(s.get("gpus") or []):
            if g.get("temp") is not None:
                lv = level_for(g["temp"], th["gpu_temp_warn"], th["gpu_temp_crit"])
                if lv or g["temp"] >= th["gpu_temp_warn"] - 5:
                    out.append(Condition(f"gpu:{i}:temp", lv, "gpu", f"GPU hot: {g['temp']:.0f}°C", g["name"], 10))
            if g.get("vram_total"):
                pct = 100 * g["vram_used"] / g["vram_total"]
                if pct >= th["vram_percent"] or (pct >= th["vram_percent"] - 5 and f"gpu:{i}:vram" in self.active):
                    out.append(Condition(f"gpu:{i}:vram", "warn" if pct >= th["vram_percent"] else None, "gpu",
                                         f"VRAM {pct:.0f}% used", g["name"], 30))
        return out

    def events(self, s: dict, now: float) -> list[Alert]:
        new = []
        changed = False
        for d in s["disks"]:
            smart = d.get("smart") or {}
            findings = smart.get("findings")
            if findings is None:
                continue
            ident = _ident(d)
            seen = self.smart_seen.setdefault(ident, {})
            for f in findings:
                key = f"{f['id']}:{f['name']}"
                before = seen.get(key)
                if before is not None and f["raw"] > before:
                    new.append(Alert(f"disk:{ident}:grew:{key}:{f['raw']}", "warn", "disk",
                                     f"{d['name']}: {f['name'].replace('_', ' ')} grew {before:,} → {f['raw']:,}",
                                     f.get("why", "") + " - the drive is getting worse", now, now, event=True))
                if before != f["raw"]:
                    seen[key] = f["raw"]
                    changed = True
        for n in s["network"]:
            if n["name"] == "lo":
                continue
            errs = n["rx_errs"] + n["tx_errs"]
            base_t, base = self.net_base.get(n["name"], (now, errs))
            if errs - base >= self.th["net_errors"]:
                new.append(Alert(f"net:{n['name']}:errors:{int(now)}", "warn", "net",
                                 f"{n['name']}: {errs - base} new network errors",
                                 f"receive {n['rx_errs']:,} / transmit {n['tx_errs']:,} errors in total "
                                 "(cable, duplex or driver trouble)", now, now, event=True))
                self.net_base[n["name"]] = (now, errs)
            elif now - base_t > 600 or n["name"] not in self.net_base:
                self.net_base[n["name"]] = (now, errs)
        if changed:
            self.save()
        return new

    # -- lifecycle ------------------------------------------------------------------------

    def evaluate(self, s: dict, now: float | None = None) -> list[Alert]:
        """Update alerts from a snapshot; returns newly raised (or escalated) alerts."""
        now = now or time.time()
        raised: list[Alert] = []
        conds = {c.key: c for c in self.conditions(s)}
        for key, c in conds.items():
            if c.level is None:
                continue
            a = self.active.get(key)
            if a:
                if LEVELS[c.level] > LEVELS[a.level]:
                    raised.append(a)
                a.level, a.title, a.detail, a.last, a.sig = c.level, c.title, c.detail, now, c.signature()
                continue
            start = self.pending.setdefault(key, now)
            if self.immediate or now - start >= c.sustain:
                a = Alert(key, c.level, c.category, c.title, c.detail, start, now, sig=c.signature())
                a.acked = self.acked.get(key) == a.sig
                self.active[key] = a
                raised.append(a)
        for key in list(self.pending):
            if key not in conds or conds[key].level is None:
                self.pending.pop(key)
        changed = bool(raised)
        for key, a in list(self.active.items()):
            if a.event or key in conds:   # still true, or still above the clear threshold
                continue
            a.resolved = now
            self.history.insert(0, self.active.pop(key))
            changed = True
        for ev in self.events(s, now):
            if ev.key not in self.active:
                self.active[ev.key] = ev
                raised.append(ev)
                changed = True
        for a in self.active.values():  # escalated / problems changed since acknowledging -> show again
            if a.acked and not a.event and self.acked.get(a.key) not in (None, a.sig):
                a.acked = False
                raised.append(a)
        if changed:
            self.save()
        return [a for a in raised if not a.acked]

    def sorted_active(self) -> list[Alert]:
        return sorted(self.active.values(), key=lambda a: (a.acked, -LEVELS[a.level], -a.first))

    def counts(self) -> dict[str, int]:
        c = {"crit": 0, "warn": 0, "info": 0}
        for a in self.active.values():
            if not a.acked:
                c[a.level] += 1
        return c

    def worst(self) -> str | None:
        levels = [a.level for a in self.active.values() if not a.acked]
        return max(levels, key=lambda l: LEVELS[l]) if levels else None

    def ack(self, key: str, value: bool = True) -> None:
        a = self.active.get(key)
        if not a:
            return
        a.acked = value
        if a.event and value:   # acknowledged events are done: move them to history
            a.resolved = time.time()
            self.history.insert(0, self.active.pop(key))
        elif value:
            self.acked[key] = a.sig
        else:
            self.acked.pop(key, None)
        self.save()

    def ack_all(self) -> None:
        for key in list(self.active):
            if not self.active[key].acked:
                self.ack(key)

    def clear_history(self) -> None:
        self.history.clear()
        self.save()
