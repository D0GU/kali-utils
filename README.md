# kali-utils

Terminal utilities for this home server, in one package. They use only the Python 3.11+ standard library, share the same full-screen curses style (↑↓ move, Shift+↑↓ jump 5, `?` help, `q` quit), and each also has plain commands for scripts.

| Tool | What it does | Manual |
|---|---|---|
| **trctl** | Transmission client: list, add, remove and track torrents; select files; per-torrent and daemon settings. `trctl watch` is the interactive view. | [docs/trctl.md](docs/trctl.md) |
| **plexsort** | Turns finished torrents (movies, TV, anime, music) into a properly named Plex library. It verifies titles online, keeps a library database, handles several library drives by free space, and can undo any sort. | [docs/plexsort.md](docs/plexsort.md) |
| **syswatch** | System monitor: CPU, RAM, GPU/VRAM, network, and disks with usage, SMART health and a per-drive report, plus alerts. | [docs/syswatch.md](docs/syswatch.md) |
| **kvctl** | Kavita manager and book importer: libraries, scans and series, plus importing finished torrents and a watched folder into a Calibre-style Books folder, with a review screen and undo. | [docs/kvctl.md](docs/kvctl.md) |
| **bookread** | E-book reader for the terminal: a menu of books, themes, margins, your place saved, and time left learned from your reading speed. | [docs/bookread.md](docs/bookread.md) |

They work together: in `trctl watch`, press `P` on a finished torrent to open it in plexsort. plexsort and kvctl reuse trctl's Transmission connection and settings.

## Install

```bash
./install.sh              # links trctl, plexsort, syswatch, kvctl and bookread into ~/.local/bin
./install.sh --remove     # removes those links
```

The links point at this folder, so changes take effect immediately. You can also `pip install .` inside a virtualenv; `pyproject.toml` defines the four commands.

First-time setup:

```bash
trctl setup                                   # Transmission address, user, password
plexsort setup                                # Plex library folders and watched folders
sudo bash ~/scripts/syswatch-enable-health.sh # optional: SMART data and drive temperatures for syswatch
kvctl setup                                   # Kavita address and API key; creates the watched folder
bookread                                      # menu of books; read in the terminal with progress and time left
```

## Layout

```
kutil/      shared formatting, curses widgets (keys, forms, dialogs, tables), text matching, HTTP and safe file moves
trctl/      Transmission RPC client, config and curses UI   (plexsort reuses its client and config)
plexsort/   parsing, online providers, music/tags, naming, placement, engine, database, curses UI
syswatch/   collectors (/proc, /sys, nvidia-smi, smartctl), alerts, curses UI
kvctl/      Kavita client, Calibre layout, book metadata readers, Open Library lookup, engine, journal, curses UI
bookread/   EPUB parsing, page layout, reading speed, curses reader and menu
bin/        launchers used by install.sh
docs/       one manual per tool
tests/      tests/trctl, tests/plexsort, tests/syswatch, tests/kvctl, tests/bookread, tests/kutil
```

## Tests

```bash
./run-tests.sh                 # everything (about 100 tests, ~35 s)
./run-tests.sh tests/syswatch  # one tool
```

The tests run offline in temporary folders, against a fake Transmission daemon, a fake Kavita server and fake data. They never touch real torrents, your library or saved state. Tests that drive the real UIs run in a detached tmux session and are skipped without tmux.

Settings and data stay where they were: `~/.config/{trctl,plexsort,syswatch,kvctl,bookread}/`, the plexsort, kvctl and bookread databases in `~/.local/share/{plexsort,kvctl,bookread}/`, and syswatch's alert state in `~/.local/state/syswatch/`.
