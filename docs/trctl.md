# trctl: Transmission CLI

A command-line client for `transmission-daemon`. It lists torrents, tracks downloads live, and manages torrent files (select/skip, priorities, move, rename, delete). It uses only the Python 3.11+ standard library and talks to the daemon's RPC API, so it works locally or over the LAN.

## Install

Part of [kali-utils](../README.md); `install.sh` links `trctl` into `~/.local/bin`.

## Configure

```bash
trctl setup            # prompts for URL, username, password; tests the connection
```

This writes `~/.config/trctl/config.toml` with mode 600:

```toml
url = "http://127.0.0.1:9091/transmission/rpc"
username = "kali"
password = "..."
# or, instead of storing the password:
# password_command = "pass show transmission"
```

Settings are resolved in this order: config file, then environment (`TRCTL_URL`, `TRCTL_USERNAME`, `TRCTL_PASSWORD`, `TRCTL_CONFIG`), then flags (`--url host[:port]`, `--user`). If a username is set and no password is available, trctl prompts for one.

## Interactive UI (`trctl watch`)

`trctl watch` (or `trctl ui`) opens a full-screen ncurses manager. It shows a live torrent list plus a details view with Info, Files, Trackers and Peers tabs, and you can do everything from there. Press `?` inside it for the full key list.

| Key | Action (applies to marked torrents, otherwise the one under the cursor) |
|---|---|
| `↑↓` `j k` `PgUp PgDn` `g G` | move · mouse wheel/click works too (double-click opens) |
| `Enter` | details (Info / Files / Trackers / Peers tabs: `Tab`, `←→`, `1-4`) |
| `Space` / `Ctrl-A` / `Esc` | mark / mark all shown / clear marks, then search |
| `/` · `Tab` · `o` `O` | live name search (globs ok) · cycle status filter · sort / reverse |
| `a` | add a magnet, URL, info hash or .torrent file (Tab completes paths) |
| `p` · `v` · `R` | start/stop · verify · reannounce |
| `d` | remove, then choose to keep the data or **delete** it |
| `m` · `n` | move data to another folder · rename the torrent |
| `c` | torrent settings: speed limits, priority, seeding ratio/idle goals, peer limit, labels |
| `[ ]` `{ }` | queue up/down · top/bottom |
| `t` · `C` | turtle mode on/off · daemon settings (limits, download folder, queue, ratio, peers) |
| `f` | jump to the Files tab |
| `P` | sort a finished torrent into Plex (opens [plexsort](../Plex%20Sorter/README.md)) |
| Files tab | `Space` want/skip · `+ -` priority · `w`/`x` want/skip all shown · `/` filter · `n` rename file · `N` rename its folder |
| Trackers tab | `a` add · `e` edit · `d` remove |
| `q` | back / quit |

Data refreshes every 2 s (`-i SECONDS` to change). `--no-mouse` leaves mouse selection to your terminal. `--plain` (also used automatically when output is piped) gives the simple auto-refreshing table instead.

To try the UI without touching your real daemon, run `python3 tests/trctl/serve_fake.py`. It prints a URL; then run `trctl --url <that URL> watch`.

## Selecting torrents

Most commands take one or more selectors:

| Selector | Example |
|---|---|
| id / range / list | `7`, `3-9`, `1,4,7-9` |
| hash prefix (6+ hex chars) | `a1b2c3` |
| name substring (case-insensitive) | `ubuntu` |
| glob on name | `'*S01*1080p*'` |
| everything | `all` |

## Commands

### Listing and tracking

```bash
trctl                          # same as `trctl list`
trctl ls -s downloading        # filter: downloading, seeding, stopped, queued, checking,
trctl ls -s done,error         #   active, complete, incomplete, error (comma = any)
trctl ls -n '*1080p*' -S size -r
trctl ls -d /mnt/data/torrents/complete -l tv
trctl ls --json | jq ...       # or --ids for just ids
trctl info ubuntu -m           # full details, trackers, magnet link
trctl stats                    # speeds, limits, totals, free disk space

trctl watch --plain            # non-interactive live table of incomplete torrents
trctl watch --plain --all -i 1 # every torrent, 1 s refresh
trctl wait 12 && notify-send done   # block until finished (exit 0 done, 1 error, 124 timeout)
trctl wait all -t 3600
```

`watch --plain` keeps a torrent on screen after it finishes during the session, so you can see that it completed.

### Adding and controlling

```bash
trctl add file.torrent 'magnet:?xt=...' https://site/x.torrent <40-char-infohash>
trctl add -p -d /mnt/data/torrents/complete/linux -l linux file.torrent
trctl start 3 | stop show | verify all | reannounce 5
trctl start --now 3            # skip the queue
trctl rm 4                     # asks for confirmation, keeps data
trctl rm -D -y 4               # remove AND delete data, no prompt
```

### File management

```bash
trctl files 7                          # list files: index, progress, size, wanted, priority
trctl files 7 -f mkv                   # only display matching files
trctl files 7 --only '*.mkv'           # download only the .mkv files, skip the rest
trctl files 7 -x sample,'*.nfo'        # skip files (index, range, glob, or name part)
trctl files 7 -w 3-5 --high 0 --low 9  # want files, set priorities

trctl mv 7 /mnt/data/torrents/complete/tv       # daemon moves the data
trctl mv --no-move 7 /mnt/data/elsewhere        # data already there; just repoint
trctl rename 7 "Nice Name"                      # rename the top-level folder/file
trctl rename 7 -p Subs subtitles                # rename a folder inside the torrent
trctl rename 7 -p 0 "Episode 1.mkv"             # rename file #0 (index from `files`)
```

Paths for `add -d` and `mv` are on the daemon's machine. On this server, AppArmor lets the daemon write only to `/mnt/data/torrents/**` (see `~/scripts/fix-transmission-apparmor.sh`). Moving data anywhere else fails with "Permission denied", which appears in `trctl info`.

## Exit codes

`0` success, `1` error, `2` authentication failed, `124` `wait` timed out, `130` interrupted.

## Development

```bash
./run-tests.sh tests/trctl      # from the kali-utils root; ./run-tests.sh runs everything
```

The tests run every command end-to-end against `tests/trctl/fake_daemon.py`. This in-process fake RPC server implements the 409 session handshake, basic auth, and the RPC methods trctl uses, so the tests never touch a real daemon. The UI test drives the real curses interface inside a detached tmux session and is skipped if tmux isn't installed.

Layout: `trctl/rpc.py` (RPC client), `trctl/config.py` (settings), `trctl/fmt.py` (formatting/tables), `trctl/cli.py` (commands), `trctl/tui.py` (interactive UI), `bin/trctl` (launcher).
