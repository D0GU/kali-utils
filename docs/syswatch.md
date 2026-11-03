# System Watch (`syswatch`)

A small terminal system monitor in the same curses style as trctl and plexsort. It shows CPU, RAM, GPU with VRAM, disks with usage and health, and network traffic. It needs no root and no extra packages, only the Python 3.11+ standard library.

```bash
syswatch                 # live dashboard
syswatch -i 2            # refresh every 2 s
syswatch --once          # one-off text summary (also used when piped)
syswatch --json          # one snapshot as JSON, for scripts
syswatch --alerts        # check alert rules once; exit 0 = none, 1 = warnings, 2 = critical (cron-friendly)
```

## Views

| Key | View |
|---|---|
| `1` | **Overview**: CPU (total, per core, frequency, temperature, load, history), memory and swap, GPU, network per interface with sparklines, disks and filesystems |
| `2` | **Disks**: per disk: health verdict and reasons, SMART summary, kernel counters, temperature, live I/O, mounts, recent kernel errors; plus all temperature sensors. **↑↓ select a drive, Enter opens its full report** |
| `3` | **Network**: every interface with addresses, speed, rates, totals, packets, errors, drops |
| `4` | **Processes**: top processes by CPU (`c`) or memory (`m`) |
| `5` | **Alerts**: active alerts and history. `↑↓` select, `a` acknowledge/un-acknowledge, `A` acknowledge all, `x` clear history, `b` terminal bell on/off |

Other keys: `↑↓ PgUp PgDn` scroll, `+`/`-` refresh speed, `Space` pause, `a` show loopback/virtual interfaces, `r` re-check slow sources, `?` help, `q` quit. Two columns from 110 characters wide, one scrolling column below that.

## Alerts

The Overview has an **Alerts** widget (top right). The header shows a `✗ critical` / `! warning` badge, and a new alert flashes in the bottom line.

| Rule | Warning | Critical |
|---|---|---|
| CPU usage | ≥ 90% for 60 s (clears below 75%) | |
| CPU temperature | ≥ 85°C for 10 s | ≥ 95°C |
| Load | ≥ 2 per thread for 2 min | |
| RAM | ≥ 90% for 30 s | ≥ 97% |
| Swap | ≥ 80% for 30 s | |
| Filesystem | ≥ 90% full | ≥ 97% full, or < 2 GiB free on volumes over 50 GiB |
| Disk health | any SMART/kernel-log warning | SMART failing / device offline |
| Disk temperature | HDD ≥ 50°C, SSD ≥ 70°C | HDD ≥ 60°C, SSD ≥ 80°C |
| SMART counters grow (reallocated, pending, …) | event, stays until acknowledged; compared with the last run too | |
| Network link goes down / ≥ 10 new errors | warning / event | |
| GPU temperature, VRAM | ≥ 85°C, VRAM ≥ 95% | ≥ 92°C |

- **Conditions** resolve by themselves and move to History. **Events** stay until you acknowledge them.
- An acknowledgement is remembered across restarts. The alert returns if it escalates, or for disk health, if the list of problems changes, e.g. more bad sectors.
- State is kept in `~/.local/state/syswatch/alerts.json`.
- Thresholds can be overridden in `~/.config/syswatch/alerts.toml` using the keys in `syswatch/alerts.py` → `DEFAULTS`, e.g. `cpu_percent = 95` or `fs_percent_warn = 85`.

## Where the numbers come from

- **CPU, memory, network, disk I/O, processes:** `/proc` (`stat`, `meminfo`, `net/dev`, `diskstats`, `[pid]/stat`).
- **Temperatures:** `/sys/class/hwmon`. CPU via coretemp/k10temp; drives via `drivetemp` (SATA) or the NVMe driver.
- **GPU:**
  - **NVIDIA:** `nvidia-smi` (usage, VRAM, temperature, power, fan, clocks, processes using VRAM).
  - **AMD:** `/sys/class/drm` (usage, VRAM, temperature, power).
  - **Other GPUs:** identified only, with a note on why there are no stats (e.g. driver not loaded).
- **Filesystems:** `/proc/mounts` + `statvfs`. LVM/RAID volumes are mapped back to their physical disk.

## Drive report (Disks → Enter)

A scrollable popup for one drive (`↑↓`, `Shift+↑↓`/`K J` 5 lines, `PgUp/PgDn`, `r` re-read, `Esc` close):

- **Verdict** and the drive's own SMART self-assessment.
- **Needs attention:** every worrying attribute with a non-zero count (reallocated, pending, uncorrectable, runtime bad blocks, CRC, timeouts …), explained in plain words.
- **Drive:** family, model, serial, firmware, capacity, sector sizes, HDD rpm or SSD, form factor, interface version and *current vs maximum link speed* (flagged if a drive runs below its max), TRIM.
- **Usage & wear:** power-on time, power cycles, start/stop and head-load cycles, data written/read, endurance used, temperature with lifetime min/max. NVMe: spare blocks, media errors, unsafe shutdowns.
- **SMART attributes:** the full table, with problem rows highlighted.
- **Self-tests and the drive's error log** (needs the full-report rule below).
- **Kernel:** device state, command and timeout counters, disk errors from the last 7 days of the kernel log.
- **Filesystems** on the drive.

## Disk health

The verdict (✓ ok / ! warning / ✗ FAILING) combines:

- **Kernel:** device state. The kernel's per-disk command-error counter is shown only as information: SMART queries (smartctl and UDisks ATA passthrough) increase it too, so it can't tell real failures apart.
- **Kernel log:** disk error messages from the last 24 h (`journalctl -k`, readable because you're in the `adm` group).
- **SMART:** the failing flag, any attribute past its threshold, and each worrying counter that isn't 0 (see the drive report), plus NVMe warnings, media errors and spare blocks. Read through UDisks2 when it provides them, or `smartctl` (see below).
- **Temperature:** warns at 55°C for HDDs and 70°C for SSDs.

To let an idle hard disk spin down, syswatch doesn't query drives constantly. SMART is read every 10 minutes and drive temperatures every minute, and a spinning disk is only queried if it has done real I/O since the last query. Values for an idle disk can therefore be older. Each start of syswatch does one first read, which can wake a sleeping disk, as can `r` and opening the drive report.

SMART needs root. To enable it, plus drive temperatures, run once:

```bash
sudo bash ~/scripts/syswatch-enable-health.sh
```

This script:
- installs smartmontools;
- adds `/etc/sudoers.d/syswatch-smartctl` and `/etc/sudoers.d/syswatch-smartctl-full`, which let your user run exactly `smartctl -j -H -A -i /dev/<disk>` and `smartctl -j -x /dev/<disk>` for each current disk (both read-only, no wildcards);
- loads the `drivetemp` module now and at boot.

Re-run it after adding a disk, or after updating syswatch to get the full report. Undo with `sudo rm /etc/sudoers.d/syswatch-smartctl /etc/sudoers.d/syswatch-smartctl-full /etc/modules-load.d/drivetemp.conf`.

## Development

```bash
./run-tests.sh tests/syswatch      # from the kali-utils root; ./run-tests.sh runs everything
```

Tests cover the parsers, health rules and panel rendering (with a fake snapshot), a live read of this machine's `/proc`/`/sys`, and a tmux smoke test of the UI. Layout: `syswatch/collect.py` (data), `syswatch/alerts.py` (alert rules and state), `syswatch/ui.py` (curses), `syswatch/cli.py`, `bin/syswatch`.
