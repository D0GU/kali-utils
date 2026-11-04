# bookread

Reads e-books in the terminal, with your place saved and a prediction of the time left. It stands alone: it needs no other tool here, only `kutil`.

```bash
bookread                  # menu of the books in your folder: books you've opened come first
bookread dune             # words from a title, author or series; one match opens, several narrow the menu
bookread ~/book.epub      # any EPUB, anywhere
bookread ~/Downloads      # menu of another folder (or: --dir FOLDER)
bookread --list [WORDS]   # print books and progress, for scripts; add --json
```

In the menu, Enter reads, `/` searches and `q` quits. Books you're reading come first, then the rest alphabetically, with a series filed under its name in reading order. Books you haven't opened show their estimated length under Left; the menu counts their words once, in the background, the first time it sees them. The reader opens EPUB files; other formats show dimmed and say so if you try. A status line at the bottom shows the chapter, page, % read, and the time left in the chapter and the book.

Pictures can't be shown, so an illustration appears as `[image]`. A picture a book uses three or more times is treated as decoration: between paragraphs it becomes a `* * *` scene break, and elsewhere it's left out. Chapters come from the book's contents list, then its headings, then short bold lines that open a file. A book with none of those has no contents and shows only the total time left.

| Keys | |
|---|---|
| Space, `→`, `↓`, PgDn | next page |
| `←`, `↑`, PgUp, `b` | previous page |
| Home, End | start and end of the book |
| `c` | contents |
| `t`, `T` | next and previous theme: Terminal, Paper, Sepia, Dark, Black, Solarized |
| `[`, `]` | smaller and bigger side margins |
| `{`, `}` | smaller and bigger top and bottom margins |
| `?` | all the keys, and your reading speed |
| `q`, Esc | close the book |

**Text size** belongs to the terminal, because a program can't set it over SSH. In Tabby, `Ctrl+=` and `Ctrl+-` zoom and `Ctrl+0` resets. The page lays itself out again and keeps your place.

**Themes** use xterm's fixed 256 colours, so Sepia and Solarized are close approximations of the usual shades. A terminal with fewer colours offers Terminal, Paper and Black. With `NO_COLOR` set, only Terminal is offered.

**Margins** are saved as a line length (72 to start), so a wide window doesn't give you 200-column lines and zooming stays readable. Theme and margins apply to every book.

**Your place** is saved on every page turn in `~/.local/share/bookread/reading.db`, as a position in the text rather than a page number. The book is recognised by what's in it, so moving or renaming it, or editing its metadata, doesn't lose your place. Progress stays on this machine and doesn't sync with Kavita or KOReader.

**Time left** starts from 238 words a minute, the average in Brysbaert's 2019 review of reading studies. Each forward page turn then gives a sample of the page's words divided by the seconds it stayed on screen, and you have one speed across all books.

- Ignored: under 60 or over 1000 words a minute (away from the screen, or flipping through), pages under 30 words, pages you've stepped back to with the previous-page key, and a page where you opened a dialog, changed the theme or changed the window size.
- Averaging: 238 counts as 20 pages' worth, so a few odd turns don't swing it. The average covers roughly your last 200 pages, so a change of speed shows up.

## Finding books

Any folder works. Files are found at any depth, and hidden folders are skipped. A book's name comes from a `metadata.opf` beside it (Calibre's layout, when the folder holds one book), then from inside the EPUB, then from a file name like `Title - Author.epub`.

## Settings

`~/.config/bookread/config.toml` is optional; `BOOKREAD_CONFIG` points to another file. Theme, margins and speed are saved for you in the reading database.

| Key | Default | Meaning |
|---|---|---|
| `books_dir` | `/mnt/data/media/Books` | the folder the menu shows |
| `database` | `~/.local/share/bookread/reading.db` | your places, speed and look |
