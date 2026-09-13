# Rekordbox ⇄ Serato Bridge

An MCP server that moves DJ library data — cues, loops, cue colors and playlists — between
**Rekordbox 6/7** and **Serato DJ Pro**, in both directions.

在 **Rekordbox 6/7** 与 **Serato DJ Pro** 之间双向搬运 DJ 资料库数据：Cue、Loop、Cue 颜色与播放列表。

Works with any MCP-compatible agent or client.

[中文说明](README.zh-CN.md) · License: [AGPL-3.0](LICENSE)

## Features

- **Both directions.** Rekordbox → Serato (a Serato-ready folder + crate) and Serato → Rekordbox
  (your local Rekordbox library, or a Rekordbox-compatible XML export).
- **Cues, loops and colors.** Hot cues, memory cues, saved loops, beat-length loops and cue colors
  are carried over into the target app's native model.
- **Works with USB exports.** Cues can be read straight from a Rekordbox device library
  (`PIONEER/USBANLZ`), so a USB stick converts without the desktop library.
- **Read-only by default.** Nothing is written until you ask for it, and audio files are only ever
  touched in staging copies.
- **Every write is previewable.** Dry runs plus a machine-readable report for each conversion.

## Requirements

| | |
| --- | --- |
| OS | macOS (tested with Rekordbox 7 and Serato DJ Pro 4.0) |
| Python | 3.9 or newer |
| Rekordbox | 6 or 7 (needed for the Serato → Rekordbox direction) |
| Serato | Serato DJ Pro 4.x |

`pyrekordbox` is installed automatically with the plugin.

## Install

### As a Codex plugin

```sh
codex plugin marketplace add shadowroommusic/rekordbox-serato-bridge
codex plugin add rekordbox-serato-bridge@shadowroom
```

### In any other MCP client

```json
{
  "mcpServers": {
    "rekordbox-serato-bridge": {
      "command": "python3",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/rekordbox-serato-bridge"
    }
  }
}
```

### CLI only

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/shadow-rb-serato --help
```

## Configuration

| Option | Default | Used for |
| --- | --- | --- |
| `--rekordbox-database` | `~/Library/Pioneer/rekordbox/master.db` | reading/writing the Rekordbox library |
| `--rekordbox-dir` | `~/Library/Pioneer/rekordbox` | Rekordbox settings + local analysis data |
| `--serato-database` | `~/Library/Application Support/Serato/Library/master.sqlite` | Serato crates and assets |
| `--device-root` | – | a Rekordbox USB device, e.g. `/Volumes/USB` |
| `--staging-dir` | – | where tagged copies are written |

## Tools

| Tool | What it does |
| --- | --- |
| `preview_rekordbox_to_serato` | Dry run: what a Rekordbox → Serato conversion would change |
| `preview_serato_to_rekordbox` | Dry run: what a Serato → Rekordbox conversion would change |
| `stage_serato_cues` | Copy a track, write Serato cues into the copy, verify it, write a manifest |
| `list_sets` | List the playlists in the local Rekordbox library |
| `convert_set` | Rekordbox set → Serato-ready folder (copies + cues/loops + `_Serato_` crate) |
| `convert_set_to_rekordbox` | Serato crate → Rekordbox (dry run, `apply`, or XML export) |

The same functionality is available from the CLI: `preview-rb-to-serato`, `preview-serato-to-rb`,
`stage-cues`, `inspect-rekordbox`, `inspect-serato`, `list-sets`, `convert-set`.

## Usage

### Rekordbox → Serato (gig folder or USB stick)

```sh
# from a Rekordbox device library (the cue data on the device is authoritative)
.venv/bin/shadow-rb-serato convert-set --to serato --name "My set" \
  --out ~/Music/ShadowRoom-USB --device-root /Volumes/USB --with-cues-only

# or from a playlist in the local library
.venv/bin/shadow-rb-serato list-sets --rekordbox-database "$HOME/Library/Pioneer/rekordbox/master.db" \
  --rekordbox-dir "$HOME/Library/Pioneer/rekordbox"
```

The output is a folder Serato understands:

```text
<out>/ShadowRoom/<set>/<tracks>      # copies carrying Serato cues/loops + BeatGrid
<out>/ShadowRoom/<set>/manifest.json
<out>/_Serato_/Subcrates/<set>.crate # the crate Serato shows in its sidebar
```

Copy the whole `<out>` folder to the root of your USB stick, or import the tracks into Serato —
either way the cues come along.

### Serato → Rekordbox

```sh
SHADOW=.venv/bin/shadow-rb-serato
RB="$HOME/Library/Pioneer/rekordbox"

# 1) dry run
$SHADOW convert-set --to rekordbox --name "My set" \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" \
  --rekordbox-database "$RB/master.db" --rekordbox-dir "$RB"

# 2) write (quit Rekordbox first; master.db is backed up automatically)
$SHADOW convert-set --to rekordbox --name "My set" --apply ...

# prefer not to touch the database? export Rekordbox-compatible XML instead
$SHADOW convert-set --to rekordbox --name "My set" --xml ~/Desktop/my-set.xml ...
```

### Previews and single-track staging

```sh
# side-by-side preview of both libraries
$SHADOW preview-rb-to-serato --rekordbox-database "$RB/master.db" --rekordbox-dir "$RB" \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" --output preview.json

# write cues into a verified staging copy only
$SHADOW stage-cues --track "/path/track.aiff" --cues cues.json --staging-dir ./staging
```

`cues.json` is either an array or `{"cues": [...]}`; each entry supports `name`, `position_ms`,
`end_ms` (loop) and `color` (`#RRGGBB` or an integer).

## Supported file formats

| Container | Read cues | Write cues |
| --- | --- | --- |
| MP3 | ✅ | ✅ |
| AIFF / AIFC | ✅ | ✅ |
| FLAC | ✅ | ✅ |
| WAV | ✅ | – |
| OGG | – | – |

## Safety

- Read-only unless you explicitly opt in (`--apply` / `"apply": true`) or point a tool at a staging
  directory.
- Writes to the Rekordbox database back up `master.db` (plus WAL/SHM) first and verify the result
  afterwards. Rekordbox must be closed while its database is written; the tool refuses otherwise.
- Audio files are never modified in place — new tags go into copies, and the report proves the
  source file is untouched.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `Rekordbox is running` | Quit Rekordbox and run the command again. |
| Cues not visible in Serato | Reload the track (Serato reads tags when a track is loaded) and make sure the crate/folder was imported. |
| Tracks listed as missing | The library points at a path that is not mounted (for example an unplugged USB stick). |
| Device tracks not found | Mount the Rekordbox device, or convert from the local library instead. |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Implementation notes for maintainers live in
[docs/internals.md](docs/internals.md).

## License

AGPL-3.0 — see [LICENSE](LICENSE). Bundled dependency `pyrekordbox` is MIT licensed.
