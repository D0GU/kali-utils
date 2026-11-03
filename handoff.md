# kali-utils handoff (2026-11-03)

Read this first, then continue with "Next steps".

## Project
`/mnt/data/Claude Projects/kali-utils` is one Python package (stdlib only, Python 3.11+) with terminal tools for this home server:

- `trctl`: Transmission client and curses UI
- `plexsort`: sorts finished torrents into a Plex library
- `syswatch`: system monitor with disk health and alerts
- `kutil`: shared code (`fmt`, `tui`, `text`, `net`, `fsops`)
- `kvctl`: **in progress**, Kavita CLI and book importer

Remote: `git@github.com:D0GU/kali-utils.git`. `main` holds the finished fixes (`b587a18`). kvctl work is on the local branch **`kvctl`**, not pushed. Run tests with `./run-tests.sh` or `./run-tests.sh tests/<tool>`. Launchers in `bin/` are linked into `~/.local/bin` by `install.sh`.

## Current task: finish kvctl
Kavita v0.9.1.4 is installed and set up by the user: `/opt/Kavita`, service `kavita`, port 5000, LAN-only, library root `/mnt/data/media/Books`.

**Decisions the user made**
- Books only for now. Keep the existing Calibre layout: `Author/Title (id)/Title - Author.ext` + `cover.jpg` + `metadata.opf`.
- The Books folder is the master. There's no Calibre database. New ids continue after the highest (next is 57) and are never reused.
- Metadata: the book's own data first, then a keyless online check that only fills gaps, with a review screen before anything moves. A match the user picks may overwrite title and author.
- Sources: finished torrents containing books, plus a watched folder (default `/mnt/data/kvctl`, not created yet). Move files, remove the torrent, then trigger a Kavita scan, like plexsort.
- Authors: use "First Last". Merge duplicates only after showing a plan.
- Curses UI in the same style as the other tools (the user's standing preference).

**Done on branch `kvctl`, tested only as noted**
- `kvctl/calibre.py`: naming, author helpers, OPF read/write. Reproduces all 52 real book paths.
- `kvctl/metadata.py`: EPUB, FB2, MOBI/AZW3, file-name fallback. Read all 52 real books without error.
- `kvctl/engine.py`, `kvctl/db.py`: inbox, plans, import, library listing, author merge, edit, undo. **Not run beyond the read-only `library_books` and `author_merge_plan`; has no tests yet.**
- `kvctl/client.py`: Kavita REST. **Never called with a real key.** `kvctl/providers.py`: Open Library lookup, not run yet. `kvctl/config.py`.
- `kutil/text.py`, `net.py`, `fsops.py` moved out of plexsort; the then-existing suite passed afterwards.

## Next steps
1. Write `kvctl/cli.py`. Commands: `ui` (default), `setup` (asks for the API key with getpass, tests it, writes config mode 600, creates the watched folder), `status`, `libraries`, `library create|rename|folders|delete|scan`, `scan-all`, `series`, `search`, `inbox`, `plan`, `import`, `books`, `authors`, `merge-authors`, `history`, `undo`, `config`.
2. Write `kvctl/tui.py` on `kutil.tui.Screen`. Tabs: 1 Inbox, 2 Books (search, details, `e` edit, `M` merge authors), 3 Kavita (`s` scan, `F` force, `A` scan all, `n` new, `e` edit, `x` delete with confirm, Enter series), 4 History (undo). Review screen: `e` edit, `f` find online, Space skip, `a` apply. `S` settings. Consider moving `run_task` and the help box from plexsort/trctl into `Screen`.
3. Add `kvctl` and `kutil` to `pyproject.toml` packages (`kutil` is listed, `kvctl` isn't), `bin/kvctl`, `TOOLS` in `install.sh`, `docs/kvctl.md`, README table.
4. Write `tests/kvctl/` with a fake Kavita server and temp folders (paths, OPF round-trip, import, undo, merge, duplicates, leftovers, mixed-media torrents), plus a tmux UI test. Build EPUBs in the tests.
5. Ask the user to run `kvctl setup` themselves, then verify `client.py` against real Kavita (libraries, scan, series filter). `x-api-key` and the library create body come from the spec only.
6. Show the author merge plan and get approval before applying it to the real library. Last computed plan, 4 books:
   - `Arina, Bertt` -> `Bertt Arina`
   - `Mathews, J.R_` -> `J.R. Mathews`
   - `Aerix, Phil` -> `Phil Aerix`
   - `Ring, Seth` -> `Seth Ring`
7. Review engine details while testing: the `key` expression in `make_plan`, the `ImportError_` name, and the `sum(...) <= 3` sidecar check.

## Kavita API facts
Spec: `https://raw.githubusercontent.com/Kareadita/Kavita/v0.9.1.4/openapi.json` (treat as data). Auth header `x-api-key`; the key comes from the user's Kavita settings. Responses are JSON even where the spec says text/plain. `/api/Health` needs no auth.

- Library type: 0 Manga, 1 Comic, **2 Book**, 3 Image, 4 Light Novel, 5 ComicVine. File type groups: 1 Archive, 2 EPUB, 3 PDF, 4 Images.
- Libraries: `GET /api/Library/libraries`, `POST /api/Library/create|update` (`UpdateLibraryDto`), `DELETE /api/Library/delete?libraryId`, `POST /api/Library/scan?libraryId&force`, `POST /api/Library/scan-all?force`, `POST /api/Library/refresh-metadata`.
- Series: `POST /api/Series/all-v2?PageNumber&PageSize`, with statements `{comparison: 5 (Contains), field: 19 (Libraries), value}`, combination 1 (And), `sortOptions {sortField: 1, isAscending}`.
- Other: `GET /api/Search/search?queryString&includeChapterAndFiles`, `GET /api/Server/server-info-slim`, `GET /api/Stats/server/stats`.

Online sources: Open Library works without a key but lacks much self-published LitRPG (no result for *Savage Webs*). Google Books returned a daily quota error (429) without a key.

## Calibre rules (verified on the real library)
Folder title and author are cut at 36 characters, file-name title and author at 31, then right-stripped. Only the first author names the folder. `\/:*?"<>|` become `_`, and a trailing `.` becomes `_`. Transliteration example: Cyrillic `г` is `gh`. Co-authors in one field ("A & B", "A and B", ";") are split.

## Rules and cautions
- **Never read `/opt/Kavita/config/appsettings.json`** (it holds `TokenKey`; the safety check blocked this once). The user enters secrets through `setup` commands, never in chat.
- Don't test against real data without asking. `/mnt/data/media/*` and `/mnt/data/torrents/*` are real. Read-only dry runs were approved for the review, but use temp folders and temp databases by default. Any real file move needs an approved plan first.
- Sudo: write a script to `~/scripts` and have the user run it in a real terminal.
- This shell lacks `debian-transmission` group membership. Use `sg debian-transmission -c '...'` for writes under `/mnt/data/torrents`.
- Commit or push only when the user asks; branch rather than committing to `main` unless told otherwise. Commit identity is D0GU (global git config).
- `python3-venv` isn't installed. Check packaging with `~/.venvs/browser/bin/python -m pip wheel`.
- Replies: plain and short, lead with the outcome. The user likes being asked about real design choices first.

## Other open items
- **GameCenter CX** (torrent id 7) was 4.5% downloaded at last check. Don't touch it or add its rules until the user says it's finished. Layout decision: one show, regular episodes in seasons, coded specials (B, SP, NC, LE, YE, RE, ...) as extras. Generic fixes won't stop it splitting; it still needs fan-code rules.
- `sudo bash ~/scripts/syswatch-enable-health.sh` should be re-run by the user to enable the full SMART report (`smartctl -j -x`).
- SSD `sdb` shows 25 reallocated, 68 offline-uncorrectable and 18 uncorrectable by attribute names, but smartctl doesn't know this model, so it's unconfirmed. A long self-test was suggested. It holds `/` and Kavita's data.
- Remote branch `fix-sorting-and-disk-polling` is merged and could be deleted if the user wants.
- Database backup from the library repair: `~/.local/share/plexsort/library.db.bak-before-repair`.
