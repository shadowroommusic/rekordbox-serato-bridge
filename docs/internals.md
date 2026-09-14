# Internals & reverse-engineering notes

Maintainer notes. Everything here was measured against real libraries (Rekordbox 7 +
Serato DJ Pro 4.0.0); the user-facing docs live in [../README.md](../README.md).

## Where cue data lives

| Source | Location | How this plugin reads it |
| --- | --- | --- |
| Rekordbox | `master.db` → `djmdCue` (memory cue / hot cue / loop) + `contentCue` JSON cache | `pyrekordbox`, read-only |
| Serato DJ Pro 4.x (MP3/AIFF) | ID3v2 GEOB frames `Serato Markers2` (v2) / `Serato Markers_` (v1) | zero-dependency ID3 parsing (MP3 head, AIFF `ID3 ` chunk, WAV `id3 ` chunk) |
| Serato DJ Pro 4.x (FLAC) | Vorbis comment `SERATO_MARKERS_V2` / `SERATO_BEATGRID` | own FLAC metadata reader/writer |
| Serato DJ Pro 4.x (crates) | `master.sqlite`: `container → location_container → container_asset → asset` | direct SQLite (4.x layout; the pre-4.x layout linked assets straight to the crate id) |

Serato's `asset.bpm` column is frequently NULL; BPM falls back to the file's `Serato BeatGrid`
terminal marker (`parse_beatgrid_bpm`).

## Serato's `master.sqlite` is a WAL database (measured 2026-09-14, Serato DJ Pro 4.0, macOS)

| Fact | Evidence |
| --- | --- |
| The library runs in WAL mode | `PRAGMA journal_mode` on a copy answers `wal` |
| `?mode=ro` cannot open it while the `-wal`/`-shm` sidecars are missing | `sqlite3.OperationalError: unable to open database file`, reproduced both on a copy and on the live file with Serato closed (SQLite would have to create the `-shm` itself) |
| `immutable=1` opens it but silently serves the last checkpoint | on the same copy it reports `no such table: asset` for a table created after that checkpoint — stale, so it must not be the fallback |
| `mode=rw` works, but SQLite then writes `-shm`/`-wal` into Serato's Library folder | observed; not acceptable — the vendor folder stays read-only for this plugin |
| Copying the database **together with** its sidecars into a temp dir reads the fresh, committed rows | 2 rows written before the copy are visible from the copy |

`readers.open_serato_library()` therefore tries `mode=ro` first and falls back to a private copy of
the database plus any `-wal`/`-shm` siblings in a temp directory, opened normally and deleted
afterwards. A library that was copied without those sidecars has no `asset` table left; the reader
turns that into an explicit message instead of a raw sqlite error.

## Rekordbox cue model (measured 2026-09-14, 16 control cues + XML export cross-check)

| Field | Meaning |
| --- | --- |
| `djmdCue.Kind = 0` | memory cue (not on a pad); memory loops exist too |
| `djmdCue.Kind = 1, 2, 3` | pad A, B, C |
| `djmdCue.Kind = 4` | **not rendered by rekordbox at all** — writing it silently loses the cue |
| `djmdCue.Kind = 5..17` | pad D … P |
| `djmdCue.OutMsec >= 0` | the entry is a loop (`POSITION_MARK Type="4"` with `End`) |
| `djmdCue.BeatLoopSize` | beat count packed as `(beats << 16) \| 1` (4 beats = 262145, 16 beats = 1048577); `0` = free-length loop |

Pad letters depend **only** on `Kind` — not on the cue's position and not on the row id order.
Write rule: nth cue → `PAD_KINDS[n]` with `PAD_KINDS = (1,2,3,5,6,…,17)` (pad D is 5, never 4).
More than 16 cues are degraded to memory cues (Kind 0) so no data is lost.

## Cue colors

`djmdCue.ColorTableIndex` is a **palette id**, not RGB (`Color` stays 255 when a color is set
through the GUI; `0` means "no color", rekordbox then auto-colors by pad slot). Measured palette
(setting each swatch in the GUI and reading the column back):

| id | color | RGB | id | color | RGB |
| --- | --- | --- | --- | --- | --- |
| 1 | Blue | `#3A59F6` | 32 | DarkYellow | `#C0B039` |
| 5 | SkyBlue | `#6BB2F9` | 38 | Orange | `#D16B32` |
| 9 | Cyan | `#66DDFB` | 42 | Red | `#D33D34` |
| 14 | Teal | `#4EA192` | 45 | Rose | `#EA387B` |
| 18 | SeaGreen | `#51AE7B` | 49 | Magenta | `#CD50C9` |
| 22 | Green | `#6DDF46` | 56 | Purple | `#A63DF6` |
| 26 | Lime | `#B2DF4A` | 60 | Violet | `#A274F7` |
| 30 | Olive | `#B6BE3D` | 62 | BlueViolet | `#6773F6` |

Direction rules: Rekordbox → Serato maps the palette id to RGB (unset → white, Serato's default);
Serato → Rekordbox picks the nearest palette entry (white/black count as "no color"), and loops
never get a color because rekordbox renders loops with its own loop color.

## Serato tag layouts (from the Mixxx implementation, verified on device)

- **CUE**: `00 | index | position(4) | 00 | R | G | B | 00 00 | label`
- **LOOP**: `00 | index | start(4) | end(4) | FF FF FF FF | 00 | R | G | B | 00 | locked | label`
  (21-byte fixed header + label + NUL; the earlier 20-byte variant we shipped is parsed for
  backwards compatibility only)
- Hot cues and saved loops use **separate index spaces** (each starting at 0); sharing one counter
  shifts every cue after a loop.
- ID3 GEOB payload = `0x0101` + base64(inner), zero-padded to 470 bytes.
- FLAC `SERATO_MARKERS_V2` = base64(prefix `application/octet-stream\0\0Serato Markers2\0`
  + `0x0101` + Serato-style base64(inner) padded to ≥470) — i.e. a double base64;
  `SERATO_BEATGRID` = base64(prefix + beat-grid blob), no double encoding.
- Serato's base64 encoder wraps every 72 characters and chops one character off the final chunk
  when it is not a multiple of 3; the decoder has to put the padding back.

## BeatGrid

- Rekordbox → Serato: BPM from `djmdContent.BPM`, anchor from the first beat of the local analysis
  file (`<rekordbox dir>/share/PIONEER/USBANLZ/…/ANLZ0000.DAT`, `beat_grid` tag); falls back to the
  first cue position, then 0.
- Serato → Rekordbox: read the terminal marker BPM and write it to the new content row; the grid
  itself is left to rekordbox's own analysis after import.

## Verification status

- Real libraries: read-only previews both ways; device `ANLZ` cue/loop → Serato `Markers2` +
  `BeatGrid`; independent cross-check of the Markers2 writer; Serato tags → Rekordbox rows
  (`djmdCue` + `contentCue` + playlist) written and read back.
- 2026-09-14 closed loop on a real machine: 16 control cues for the pad table, 4/16-beat and
  hand-drawn free-length loops, a rekordbox loop showing up in Serato's saved-loop list, the same
  loop coming back as a yellow rekordbox loop (`BeatLoopSize = 262145`), and all 16 palette colors
  round-tripping.
- Not implemented: OGG (Serato DJ Pro does not support it, and Mixxx has no Serato tag reader for
  it either); WAV writing (read-only); writing a grid back into rekordbox.
