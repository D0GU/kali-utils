# kali-utils handoff (2026-11-03)

Read this first, then continue with "Next steps".

## Project
`/mnt/data/Claude Projects/kali-utils` is one Python package (stdlib only, Python 3.11+) with terminal tools for this home server:

- `trctl`: Transmission client and curses UI
- `plexsort`: sorts finished torrents into a Plex library
- `syswatch`: system monitor with disk health and alerts
- `kutil`: shared code (`fmt`, `tui`, `text`, `net`, `fsops`)
- `kvctl`: **in progress**, Kavita CLI and book importer
- `bookread`: terminal e-book reader

Remote: `git@github.com:D0GU/kali-utils.git`. kvctl work was merged into `main` on 2026-11-04. Run tests with `./run-tests.sh` or `./run-tests.sh tests/<tool>`. Launchers in `bin/` are linked into `~/.local/bin` by `install.sh`.

## Current task: finish kvctl
Kavita v0.9.1.4 is installed and set up by the user: `/opt/Kavita`, service `kavita`, port 5000, LAN-only, library root `/mnt/data/media/Books`.

**Decisions the user made**
- Books only for now. Keep the existing Calibre layout: `Author/Title (id)/Title - Author.ext` + `cover.jpg` + `metadata.opf`.
- The Books folder is the master. There's no Calibre database. New ids continue after the highest (next is 57) and are never reused.
- Metadata: the book's own data first, then a keyless online check that only fills gaps, with a review screen before anything moves. A match the user picks may overwrite title and author.
- Sources: finished torrents containing books, plus a watched folder (default `/mnt/data/kvctl`, not created yet). Move files, remove the torrent, then trigger a Kavita scan, like plexsort.
- Authors: use "First Last". Merge duplicates only after showing a plan.
- Curses UI in the same style as the other tools (the user's standing preference).

**State (2026-11-04): built, tested, pushed and merged to `main`, with the user's approval.** Commit or push only when the user asks. `./run-tests.sh`: 597 pass (including bookread). `kvctl` is linked into `~/.local/bin`. The author merge, series grouping and Overlord renumbering were applied to the real library (see Next steps); nothing else there changed. `/mnt/data/kvctl` and `~/.config/kvctl` don't exist until `kvctl setup` runs.

- Added: `kvctl/cli.py`, `kvctl/tui.py`, `bin/kvctl`, `docs/kvctl.md`, `tests/kvctl/`, README and packaging entries. `Screen.run_task` and `Screen.show_text` now live in `kutil/tui.py`; plexsort and trctl use them.
- Engine fixes beyond the plan: mixed torrents keep media and the torrent; a path named by hand keeps its other files; all-skipped plans are refused; sidecars only apply when a folder holds one book; undo checks everything before moving; symlinks ignored; ISBN and publisher no longer copied from Open Library (work-level results, arbitrary edition; it returned a Portuguese ISBN for Neuromancer). `ImportError_` is now `PlanError`.
- Verified against live Open Library through the UI: auto match, `f` pick, cover download. Not verified: anything needing a real API key.

## bookread (e-book reader, uncommitted on branch `kvctl`)
Standalone tool in `bookread/`, manual in `docs/bookread.md`, tests in `tests/bookread/`. It must not import kvctl or trctl (a test checks this); kvctl imports it for `kvctl read` and `r` on the Books tab (`tests/kvctl/test_read.py`). User's decisions: terminal reader; font size is Tabby's own zoom (the user runs Tabby on Windows over ssh.exe, and xterm.js lets no app set it); progress stays local and separate from KOReader, which the user reads on over OPDS. If that changes, Kavita has KOReader sync endpoints (`/api/Koreader/{apiKey}/syncs/progress`).

Moved out of kvctl on the user's request. Two books' progress was copied to `~/.local/share/bookread/reading.db`; the old `reading` table in kvctl's database is untouched and unused, so it can be dropped once the user is happy.

Checked read-only against the real library with the user's approval: all 51 EPUBs parse; ids 5, 37 and 38 aren't EPUB and are refused. Id 9 is 4.6M words: 8.7 s to open, 1.2 s to re-lay out, 355 MiB; the others open in under 0.7 s. Page turns take about 4 ms. Tested in tmux with `xterm-256color`, not in Tabby itself, so italics and colours there are unconfirmed until the user tries it.

## Code review fixes (2026-11-04, committed on `kvctl`)
All 10 findings from the suite review are fixed. Behaviour changes to remember:
- Imports only clear leftovers known to be junk (`engine.JUNK_EXT`); any other file keeps the whole source and its torrent.
- Cleared files go to `holding_dir` (default `/mnt/data/kvctl-holding`, created when first needed) per operation for 30 days, journal kind `held`, and undo restores them. The `leftovers_deleted` column now only marks older imports that really deleted.
- A skipped duplicate in a format the library lacks stays in the source (`Book.new_formats`), and its torrent stays.
- Shared in kutil: `media.py`, `text.clean_text`, `tui.set_locale`. `fmt.table` cleans every cell.

plexsort has the same cleanup now, with `holding_dir` default `/mnt/data/plexsort-holding` and table `held`. GameCenter CX is done in code: files named `<torrent name> [Sxx] [-] CODE - Name` are extras of the show (`parse.CODED_SPECIAL_RE`). The user imported it on 2026-11-04 as operation 16: 297 episodes and 67 specials, verified. Two files the torrent misnumbered were renamed with the user's approval: `s20e17 - F-Zero X` and `s06e08 - Bio Miracle Bokutte Upa`. 14 numbers stay shared on purpose (alternate cuts). How Plex shows it wasn't checked.

Review findings left unfixed: trctl search/filter cursor crash in narrow windows, raw torrent names in direct prints, Files tab summary covering the last row, `[` in names treated as globs, `setup --password-stdin`; syswatch false FAILING from `when_failed: past`, de-escalation re-raising, live temperature in the alert signature, unvalidated config and state types, percent ignoring reserved blocks.

## Next steps
Done 2026-11-03 at the user's request: `kvctl/epub.py` writes authors and series inside EPUBs (import, edits, merge, `fix-series`; journal kind `epub`, undo restores); `kvctl/series.py` works out series from titles, and a guess only counts if it joins two books by one author. Merge applied as operation 1 on 6 books. Kavita splits Writer on commas (`ComicInfo.cs` `SplitNames`), which is why `Ring, Seth` was wrong. Backup of those 6 books' original folders: `~/.local/share/kvctl/backup-before-author-merge-2026-11-03.tar` (delete once the user is happy).

The user ran `kvctl setup`; `client.py` now works against real Kavita (status, series, library scan, plus `/api/Series/volumes` by script). The real library is named `kali`. `search` keys still unchecked unless noted below.

Dungeon Diving bug, fixed 2026-11-03. Two faults, both found on the real server:
- `suggest_series` gave a new book a series but left the library book it grouped with untouched, so Kavita showed two series. Imports now bring those books along (`Plan.series_partners`, shown in the review and in `plan`, same journal operation).
- A normal Kavita scan removed volume 101 although its file exists. A one-book series records that book's folder as its home (`LowestFolderPath`), the folder looks unchanged, so only the new book is parsed and `ProcessSeries.RemoveVolumes` drops the other. `scan_kavita` now always forces. Series that already span folders record the author folder and are safe. A dropped volume loses its Kavita record, so any reading progress on it goes too.

3. Applied 2026-11-03 with the user's approval: operation 2 grouped 12 books into series (Apocalypse Cultivation, Battle Mage Farmer, Combat Healer, Jake's Magical Market, Reincarnated as a Slime, The Saga of Tanya the Evil); operation 3 renumbered Overlord Vols. 15 and 16 (were both 14). Confirmed in Kavita. Operation 6 put Dungeon Diving 102 in its series. Backup of those 14 folders: `~/.local/share/kvctl/backup-before-series-2026-11-03.tar`. Decided: Overlord's joint name `Kugane Maruyama & so-bin` stays together; titles aren't written inside EPUBs. Undo refuses if a later operation changed the same books, so operation 1 can only be undone after 2.
4. 3 books can't appear in Kavita because of format: id 38 (.mobi), id 5 (.azw3), id 37 Neuromancer (.fb2). Some author values look odd (`Cebelius -`, `Inadvisably Compelled`, `Emperor of Rome Marcus Aurelius`).
5. Commit when asked. Merge to `main` and push only when told.

## Also uncommitted: plexsort
`clean_title` cut episode and extra names at any year (`At Anime Expo 2018 Interview...` became `At Anime Expo`, and two Overlord extras collided). They now pass `keep_years`; movie and show titles still end at the year.

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
- `sudo bash ~/scripts/syswatch-enable-health.sh` should be re-run by the user to enable the full SMART report (`smartctl -j -x`).
- SSD `sdb` shows 25 reallocated, 68 offline-uncorrectable and 18 uncorrectable by attribute names, but smartctl doesn't know this model, so it's unconfirmed. A long self-test was suggested. It holds `/` and Kavita's data.
- Remote branch `fix-sorting-and-disk-polling` is merged and could be deleted if the user wants.
- Database backup from the library repair: `~/.local/share/plexsort/library.db.bak-before-repair`.
