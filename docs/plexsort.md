# Plex Sorter (`plexsort`)

Turns completed Transmission torrents into a properly named Plex library: movies, TV, anime and music. It works out what each file is (episode, special/OVA, movie, extra, album track, cover art, junk), verifies the titles online, shows you the plan in a curses UI to review and correct, then:

1. moves the files into Plex's folder and file naming,
2. removes the torrent from Transmission (the data is kept, because it was moved),
3. deletes the leftover junk (.txt/.nfo/samples), and
4. records everything in a SQLite library database, so any sort can be undone.

Part of [kali-utils](../README.md). It reuses trctl's Transmission connection (`~/.config/trctl/config.toml`) and the shared `kutil` curses widgets.

## Plex layout produced

```
/mnt/data/media/TV Shows/Gurren Lagann (2007) {tvdb-80096}/Season 01/Gurren Lagann (2007) - s01e01 - Bust Through the Heavens with Your Drill!!.mkv
                                                          /Season 00/...            (specials / OVAs)
                                                          /Other/NCOP 01.mkv        (extras: Trailers, Featurettes, Other, ...)
/mnt/data/media/Movies/Ninja Scroll (1993) {imdb-tt0107692}/Ninja Scroll (1993).mkv
                                                           /Ninja Scroll (1993).en.srt
/mnt/data/media/Music/Radiohead/OK Computer (1997)/01 - Airbag.flac
                                                  /folder.jpg, rip.log          (artwork and rip logs travel with the album)
/mnt/data/media/Music/Pink Floyd/The Wall (1979)/2-01 - Hey You.flac            (multi-disc)
/mnt/data/media/Music/Various Artists/Hits (2001)/02 - Artist B - Song B.mp3    (compilations)
```

The `{tvdb-…}` / `{tmdb-…}` / `{imdb-…}` tags make Plex match the exact title. In Plex, create a **Movies**, a **TV Shows** and a **Music** library, and add every folder you configure for that type to it. Plex merges a library's folders, so a show split across two drives still appears as one show.

## Folders and drive space

The first time you start `plexsort` (or with `plexsort setup`, or Settings → folders), you choose:

- **Watched folders:** extra folders whose contents appear in the Inbox. Transmission's download folder is always included.
- **Library folders:** one or more each for Movies, TV Shows and Music, e.g. one per drive. The first in each list (★) is preferred. Reorder with `[` `]`.

The folder browser shows each drive's free space. `Tab` lists the mounted drives, `n` creates a folder, `g` jumps to a typed path.

Before anything is moved, every title is given a library folder:

1. a folder you picked by hand (`l` in the review screen);
2. otherwise a folder that **already has that title**, if it has room, so new seasons and albums join the existing ones;
3. otherwise the **first folder in your list with enough room**.

"Enough room" means the title fits while still leaving `min_free_gb` (default 2 GB) free. Space promised to other titles in the same sort is counted too (largest titles are placed first). Moving within the drive the download is on is a rename and needs no extra space. If no folder has room, the review screen shows "✗ no room on any drive" and sorting is blocked. The apply dialog shows, per destination drive, what will be written and how much space will be left.

## Online verification

| Provider | Used for | Key |
|---|---|---|
| TVmaze | TV shows, episode lists and titles, TVDB/IMDb ids, popularity (tie-breaks) | none |
| Wikidata | movies and TV shows, with IMDb/TMDB/TVDB ids | none |
| AniList | anime series, films, OVAs (romaji + English titles) | none |
| MyAnimeList (Jikan) | anime fallback when AniList finds nothing | none |
| anime-lists (Fribb) | maps AniList/MAL ids → TVDB/TMDB/IMDb for Plex | none (cached, refreshed every 2 weeks) |
| MusicBrainz | albums (every edition) and tracklists | none |
| Cover Art Archive | album covers for MusicBrainz releases, downloaded if an album has none | none |
| TMDB | optional extra source for movies/TV | free key: themoviedb.org/settings/api, then `plexsort config set tmdb_api_key KEY` |

Everything works without any API key. A TMDB key just adds another source.

Each detected title is searched automatically and accepted only when it scores ≥ 0.85 with a clear lead. Anything else is marked **not verified**, and you pick the match yourself (`f`). For shows, the episode list is fetched to fill in episode titles, to map absolute anime numbering onto seasons, and to flag episode numbers that don't exist. Results that two providers agree on get a small bonus. A country suffix (`The Office US`) is used as a hint. All responses are cached in the database.

## Music

Audio files (FLAC, MP3, M4A/AAC/ALAC, Ogg, Opus, WAV, AIFF, WMA, APE, WavPack, DSD) become album tracks:

- **Tags first.** Artist, album artist, album, year, disc and track numbers come from the embedded tags. They're read by a small built-in reader (FLAC/Vorbis, ID3v1/v2.2–2.4, MP4, Ogg/Opus), so there's nothing to install.
- **Names as fallback.** Untagged files are worked out from folder names (`Artist - Album (Year) [FLAC 24-96]`, `Artist/Album/CD1/…`, `VA - …`) and file names (`01 - Title`, `1-03 Title`, `101 Title`).
- **Compilations** (tagged as such, or with several track artists and no album artist) go under *Various Artists*, and the track artist goes in the file name.
- **Discs.** Multi-disc albums are flattened into one folder with `1-01`, `2-01` numbering. Cover images, scans and `.cue`/`.log` files go with the album. A single loose image becomes `cover.jpg`.
- **MusicBrainz verification** picks the release with the right artist, album, year and track count (CD/digital editions win ties). It then fixes track titles and numbers from the official tracklist and flags tracks that aren't on the release.
- **Tags are not rewritten.** Plex reads tags itself; plexsort only fixes folder and file names.

## The UI (`plexsort`)

Tabs: **1 Inbox** (completed torrents, plus any folders listed in `inbox_dirs`) · **2 Library** · **3 History**. Press `?` for every key.

- **Inbox:** `Enter` analyses a torrent and opens the review screen. `o` opens any other folder or file.
- **Review:** each title gets a header row (library, Plex folder, match status), followed by every file as source → destination with ✓ / ! / ✗.
  - `e`/`Enter` edits a title (name, year, ids, Movies vs TV), an album (artist, album, year, MusicBrainz release id), a single file (kind, season, episode, episode title, part, extra type), or a track (disc, number, title, track artist).
  - `f` searches online, with a provider and type picker (TV show / Movie / Album); for albums type `Artist - Album`. `i` in that dialog enters ids by hand.
  - `Space` skips or includes a file. `m` marks files and `E` bulk-edits them, e.g. "renumber from 1".
  - `M` re-runs auto-matching. `l` changes which library folder/drive a title goes to. `a` applies, with options to remove the torrent and delete leftovers.
  - Nothing is moved until you apply. Problems marked ✗ (duplicate or existing destination, missing episode number, unfinished `.part` files) block applying.
- **Library:** browse movies and shows, `Enter` for files, `f` re-match online / `e` rename by hand (renames the folder and files on disk), `u` undo, `x` forget, `s` scan library folders (indexes files you put there yourself, flags deleted ones).
- **History:** every sort operation. `u` moves the files back, optionally re-adding the torrent to Transmission, which then re-checks the data.

From trctl's interactive view (`trctl watch`), press `P` on a finished torrent to open it here directly.

## Commands

```bash
plexsort                         # the UI
plexsort inbox                   # completed torrents waiting to be sorted
plexsort plan 12                 # dry run for torrent 12 (or a name, hash, or path)
plexsort apply 12 -y             # sort it (refuses if any title is unverified, unless --allow-unverified)
plexsort library [-m|-s|-M] [-q text]   # movies / shows / music
plexsort show TITLE_ID           # files of a title
plexsort history                 # operations
plexsort undo OP_ID [--readd]    # move files back (and re-add the torrent)
plexsort scan                    # index existing library folders, detect deleted files
plexsort search "frieren" --anime
plexsort search "OK Computer" --music --artist Radiohead
plexsort setup                   # choose library + watched folders (curses)
plexsort config                  # show settings, folders and their free space
plexsort config add movies_dirs /mnt/disk2/Movies     # also: tv_dirs, music_dirs, inbox_dirs
plexsort config remove inbox_dirs /some/folder
plexsort config set min_free_gb 5  # other keys: database, tmdb_api_key, id_tags, remove_torrent,
                                   # delete_leftovers, online, download_cover_art
```

`--offline` skips online lookups. `--url/--user` override the Transmission connection.

## Permissions

Torrent folders belong to `debian-transmission` (group-writable). Your user must be in that group to move files out of them. `kali` was added to it, but the membership only takes effect in a **new login session**. If sorting fails with "Permission denied", log out and back in. Files keep their original owner, and Plex reads them through the same group.

## Development

```bash
./run-tests.sh tests/plexsort      # from the kali-utils root; ./run-tests.sh runs everything
```

The tests run offline in temp folders against trctl's fake Transmission daemon, including a tmux-driven run of the real curses UI (review → edit → apply → undo).

Layout: `parse.py` (filename heuristics), `music.py` (albums/tracks), `space.py` (drive-space placement), `folders_ui.py` (folder manager + browser), `tags.py` (audio tag reader), `providers.py` (online lookups + scoring), `naming.py` (Plex paths), `engine.py` (inbox, plan checks, move/undo/retitle/scan), `db.py` (SQLite), `tui.py`, `cli.py`.
