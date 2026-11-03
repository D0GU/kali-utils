"""Disk-space-aware placement: decide which library folder (drive) each title goes to.

Rules, per title (largest titles are placed first):
1. a folder the user picked by hand wins (but must still fit);
2. otherwise, among the folders with enough room, prefer one that already holds this title
   (keeps a show's seasons / an artist's albums together), then the configured order;
3. moving within the same filesystem as the download needs no extra space (it's a rename);
4. `min_free_gb` is always kept free; space promised to earlier titles in the same sort is reserved.
If no folder has room, the plan gets an error and can't be applied.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from . import naming
from .parse import Group, Plan

GB = 1024 ** 3
PSEUDO_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs", "pstore", "bpf",
             "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "overlay",
             "squashfs", "nsfs", "ramfs", "efivarfs", "rpc_pipefs", "fuse.portal", "fuse.gvfsd-fuse"}


def existing_ancestor(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def drive_info(path: str) -> tuple[int, int, int]:
    """(device id, free bytes, total bytes) of the filesystem that `path` is (or would be) on."""
    anchor = existing_ancestor(path)
    usage = shutil.disk_usage(anchor)
    return os.stat(anchor).st_dev, usage.free, usage.total


def mounts() -> list[tuple[str, str, int, int]]:
    """Real mounted filesystems: (mount point, fs type, free, total), largest first."""
    out, seen = [], set()
    try:
        with open("/proc/mounts") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt, fstype = parts[1].replace("\\040", " "), parts[2]
        if fstype in PSEUDO_FS or mnt.startswith(("/proc", "/sys", "/dev", "/run", "/snap", "/boot/efi")):
            continue
        try:
            dev = os.stat(mnt).st_dev
            usage = shutil.disk_usage(mnt)
        except OSError:
            continue
        if dev in seen or usage.total == 0:
            continue
        seen.add(dev)
        out.append((mnt, fstype, usage.free, usage.total))
    return sorted(out, key=lambda m: -m[3])


def source_device(path: str) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


@dataclass
class Candidate:
    root: str
    device: int
    free: int
    total: int
    cost: int          # bytes this title would take on that drive (0 when it's a same-drive rename)
    available: int     # free - already reserved - safety margin
    existing: bool     # title folder already exists there

    @property
    def fits(self) -> bool:
        return self.cost <= self.available


@dataclass
class Placement:
    key: tuple
    category: str
    label: str
    need: int
    root: str
    candidates: list[Candidate]
    reason: str
    fits: bool
    manual: bool

    def chosen(self) -> Candidate | None:
        return next((c for c in self.candidates if c.root == self.root), None)


def group_bytes(g: Group, plan: Plan) -> int:
    base = os.path.dirname(plan.root) if os.path.isfile(plan.root) else plan.root
    total = 0
    for it in g.items:
        total += it.size
        for rel in [s.src for s in it.subs] + [src for src, _ in it.art]:
            try:
                total += os.path.getsize(os.path.join(base, rel))
            except OSError:
                pass
    return total


def place(plan: Plan, cfg: dict) -> list[Placement]:
    margin = int(float(cfg.get("min_free_gb", 2)) * GB)
    src_dev = source_device(plan.root)
    reserved: dict[int, int] = {}
    info_cache: dict[str, tuple[int, int, int]] = {}
    groups = plan.groups()
    sizes = {id(g): group_bytes(g, plan) for g in groups}
    placements = []
    for g in sorted(groups, key=lambda g: -sizes[id(g)]):
        need = sizes[id(g)]
        roots = naming.library_roots(g.category, cfg)
        manual = next((it.root for it in g.items if it.root_manual and it.root), None)
        if manual and manual not in roots:
            roots = roots + [manual]
        cands = []
        for r in roots:
            if r not in info_cache:
                try:
                    info_cache[r] = drive_info(r)
                except OSError:
                    continue
            dev, free, total = info_cache[r]
            cost = 0 if dev == src_dev else need
            cands.append(Candidate(r, dev, free, total, cost, free - reserved.get(dev, 0) - margin,
                                   os.path.isdir(os.path.join(r, naming.title_dir_name(g.items[0], cfg)))))
        fitting = [c for c in cands if c.fits]
        preferred = cands[0] if cands else None
        if manual:
            chosen = next((c for c in cands if c.root == manual), None)
            reason = "chosen by you"
        else:
            chosen = next((c for c in fitting if c.existing), None)
            reason = "already has this title"
            if chosen is None and fitting:
                chosen = fitting[0]
                reason = ("preferred folder" if chosen is preferred else
                          f"preferred drive too full ({_gb(preferred.available + margin)} free)")
            if chosen is None:
                chosen = max(cands, key=lambda c: c.available, default=None)
        if chosen and chosen.cost == 0 and chosen.fits:
            reason += " · same drive as download (instant move)"
        fits = bool(chosen and chosen.fits)
        if chosen and fits:
            reserved[chosen.device] = reserved.get(chosen.device, 0) + chosen.cost
        if chosen:
            for it in g.items:
                it.root = chosen.root
        label = naming.display_folder(g.category, g.title, g.year, {}, g.artist, False)
        if not fits:
            most = max(cands, key=lambda c: c.available, default=None)
            reason = (f"needs {_gb(need)}, but no {g.category} folder has room"
                      + (f" (most: {_gb(max(0, most.available))} usable in {most.root})" if most else
                         " (no library folder configured)"))
        placements.append(Placement(g.key, g.category, label, need, chosen.root if chosen else "", cands, reason, fits,
                                    bool(manual)))
    return placements


def _gb(n: int) -> str:
    from trctl import fmt
    return fmt.human_size(max(0, n))
