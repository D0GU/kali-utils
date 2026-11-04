# kvctl

Manages a Kavita server and imports books into a Calibre-style folder. `kvctl` opens the curses UI; every job also has a plain command.

## Setup

```bash
kvctl setup     # asks for the Kavita address and API key, tests them, saves with mode 600, creates the watched folder
kvctl status    # checks Kavita, the key, the folders and Transmission
```

The API key comes from your Kavita user settings and needs an admin account. It's only entered through `kvctl setup`, so it stays out of your shell history. `kvctl config` shows whether it's set, never its value.

## The Books folder

The folder is the master copy; there is no Calibre database. Each book looks like this:

```
Author/Title (id)/Title - Author.epub
Author/Title (id)/cover.jpg
Author/Title (id)/metadata.opf
```

Names follow Calibre's rules: folder titles and authors are cut at 36 characters, file-name ones at 31, and `\/:*?"<>|` become `_`. Only the first author names the folder. New ids continue after the highest ever used and are never reused.

## Importing

Sources are finished torrents that hold books, and anything in the watched folders (default `/mnt/data/kvctl`).

```bash
kvctl inbox                # what's waiting
kvctl plan SOURCE          # what would happen; changes nothing
kvctl import SOURCE        # shows the plan, asks, then does it
kvctl undo OP              # reverses it; `kvctl history` lists operations
```

`SOURCE` is a torrent id, hash or name, an inbox name, or a path. Use `--offline` before the command to skip online checks.

**Metadata** comes from the book itself (a `metadata.opf` beside it, then the EPUB, FB2 or MOBI data, then the file name). A keyless Open Library check then fills gaps: the year and a cover if the book has none. It never replaces a title or author on its own, but a match you pick with `f` on the review screen may. Self-published books are often missing from Open Library, so "not found online" is only a warning.

**Duplicates** of a book already in the library start skipped; press Space to import a second copy. A skipped duplicate in a format the library doesn't have is kept in the source, not deleted, and its torrent stays.

**What happens to the source** is shown before you confirm:

| Source | Other files and skipped books | Torrent |
|---|---|---|
| Torrent or watched folder | deleted | removed from Transmission (the moved books stay) |
| Torrent that also holds anything else (videos, audiobooks, archives, unknown files) | all kept | kept, stopped |
| Path you named (`o` in the UI, or a path argument) | all kept; only the books move | - |

`--keep-torrent`, `--keep-leftovers` and `--remove-torrent` override this. Files an import deleted can't be restored by undo. Settings `remove_torrent` and `delete_leftovers` change the defaults.

A source with `.part` files, or a book with no title or author, can't be applied until that's fixed.

## The UI

| Tab | Keys |
|---|---|
| **1 Inbox** | Enter review, `o` open any folder, `r` refresh |
| **Review** | `e` edit, `f` find online, Space skip, `a` apply, Esc back (keeps your changes) |
| **2 Books** | Enter details, `r` read, `/` search, `e` edit, `M` merge authors, `s` group series |
| **3 Kavita** | Enter series, `s` scan, `F` force scan, `A` scan all, `n` new, `e` edit, `x` delete, `r` refresh |
| **4 History** | Enter changes, `u` undo |

`S` opens settings, `?` shows help and `q` goes back or quits.

## Reading

`kvctl read` and `r` on the Books tab hand books to [bookread](bookread.md). `kvctl read WORDS` searches this library, by id or by title, author and series; `kvctl read` alone shows bookread's menu of the Books folder.

## Books and authors

```bash
kvctl books [-q WORDS] [--author NAME] [--json]
kvctl authors
kvctl merge-authors            # shows the plan to make every author "First Last", with one folder and one spelling
kvctl merge-authors --apply    # shows it again, asks, then renames
```

## Series

```bash
kvctl fix-series               # shows books Kavita would split up, with their series and number
kvctl fix-series --apply       # shows it again, asks, then writes them into the books
```

A book joins a series when its title states one (`Vol. 2`, `(Saga Book 2)`, `Saga 2: Subtitle`) and that puts it with another book by the same author, or when its title or subtitle is an existing series name. A book with no number takes the free place, shown as "number guessed", so check those. Existing series names and numbers are never changed.

Importing does the same, so a new volume joins its series; the review screen shows it with a warning, and `e` changes it. If that series needs a book already in the library, that book joins too. The review lists it before you apply, and undoing the import takes it back out.

Merges, series changes and edits are journalled and can be undone. Undo refuses to run if a later operation changed the same books; undo that one first.

## Kavita

```bash
kvctl libraries
kvctl library create NAME FOLDER...
kvctl library rename LIBRARY NAME
kvctl library folders LIBRARY [--add FOLDER] [--remove FOLDER]
kvctl library delete LIBRARY    # Kavita forgets it and its reading progress; files stay on disk
kvctl library scan LIBRARY [--force]
kvctl scan-all [--force]
kvctl series [LIBRARY]
kvctl search QUERY              # or: kvctl search --online TITLE [--author NAME]
```

`LIBRARY` is an id or name. After an import, merge, edit or undo, kvctl asks Kavita for a forced scan of the library holding the Books folder. If that fails, the change still stands.

A forced scan matters here. Each book has its own folder, and a normal scan skips folders that look unchanged. If a second book joins a one-book series, a normal scan reads only the new folder and removes the first book from Kavita, though its file is still on disk. If a volume goes missing, `kvctl library scan LIBRARY --force` (`F` in the UI) brings it back.

## What Kavita shows

Kavita reads a book's title, author and series from inside the EPUB, not from `metadata.opf`. That has two effects:

- It splits authors at commas, so `Ring, Seth` shows up as two people, "Ring" and "Seth".
- It groups books into a series by the series name and number in the EPUB, and ignores a series with no number. Without both it falls back to the title, which usually makes each book its own series.

So kvctl also writes authors and series inside the EPUB when it imports a book, when you change them, when you merge authors, and when you group series. Only those entries change; every other part of the file is checked as unchanged before the new file replaces the old one, and undo puts the old entries back. Other formats stay as they are.

A joint name the library already uses, like `Kugane Maruyama & so-bin`, stays one entry, and a new book listing the same people separately is joined up to match.

Titles are not written inside the EPUB, so changing a title here doesn't change what Kavita shows.

Kavita also only opens EPUB and PDF, so FB2 and MOBI books are stored here but don't appear there.

## Settings

`~/.config/kvctl/config.toml`, edited with `S` in the UI or `kvctl config set KEY VALUE`. `KVCTL_CONFIG`, `KVCTL_URL` and `KVCTL_API_KEY` override it for one run. The journal is at `~/.local/share/kvctl/library.db`.

| Key | Default | Meaning |
|---|---|---|
| `url` | `http://127.0.0.1:5000` | Kavita address |
| `books_dir` | `/mnt/data/media/Books` | the Books folder |
| `inbox_dirs` | `/mnt/data/kvctl` | watched folders, comma-separated |
| `library_id` | `0` | Kavita library to scan; 0 is the one holding `books_dir` |
| `online` | `true` | check books on Open Library |
| `scan_after_import` | `true` | ask Kavita to scan after changes |
| `remove_torrent` | `true` | remove the torrent after importing |
| `delete_leftovers` | `true` | delete other files in the source after importing |
| `min_free_gb` | `2` | space to keep free when importing across drives |
