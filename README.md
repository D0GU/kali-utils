# kali-utils

Terminal utilities for this home server, in one package. They use only the Python 3.11+ standard library, share the same full-screen curses style (↑↓ move, Shift+↑↓ jump 5, `?` help, `q` quit), and each also has plain commands for scripts.

| Tool | What it does | Manual |
|---|---|---|
| **trctl** | Transmission client: list, add, remove and track torrents; select files; per-torrent and daemon settings. `trctl watch` is the interactive view. | [docs/trctl.md](docs/trctl.md) |
| **plexsort** | Turns finished torrents (movies, TV, anime, music) into a properly named Plex library. It verifies titles online, keeps a library database, handles several library drives by free space, and can undo any sort. | [docs/plexsort.md](docs/plexsort.md) |
| **syswatch** | System monitor: CPU, RAM, GPU/VRAM, network, and disks with usage, SMART health and a per-drive report, plus alerts. | [docs/syswatch.md](docs/syswatch.md) |

They work together: in `trctl watch`, press `P` on a finished torrent to open it in plexsort. plexsort reuses trctl's Transmission connection and settings.

## Install

```bash
./install.sh              # links trctl, plexsort and syswatch into ~/.local/bin
./install.sh --remove     # removes those links
```

The links point at this folder, so changes take effect immediately. You can also `pip install .` inside a virtualenv; `pyproject.toml` defines the three commands.

First-time setup:

```bash
trctl setup                                   # Transmission address, user, password
plexsort setup                                # Plex library folders and watched folders
sudo bash ~/scripts/syswatch-enable-health.sh # optional: SMART data and drive temperatures for syswatch
```

## Layout

```
trctl/      Transmission RPC client, config, formatting, curses UI   (other tools reuse rpc/config/tui widgets)
plexsort/   parsing, online providers, music/tags, naming, placement, engine, database, curses UI
syswatch/   collectors (/proc, /sys, nvidia-smi, smartctl), alerts, curses UI
bin/        launchers used by install.sh
docs/       one manual per tool
tests/      tests/trctl, tests/plexsort, tests/syswatch
```

## Tests

```bash
./run-tests.sh                 # everything (about 100 tests, ~35 s)
./run-tests.sh tests/syswatch  # one tool
```

The tests run offline in temporary folders, against a fake Transmission daemon and fake data. They never touch real torrents, your library or saved state. Tests that drive the real UIs run in a detached tmux session and are skipped without tmux.

Settings and data stay where they were: `~/.config/{trctl,plexsort,syswatch}/`, plexsort's library database in `~/.local/share/plexsort/`, and syswatch's alert state in `~/.local/state/syswatch/`.
