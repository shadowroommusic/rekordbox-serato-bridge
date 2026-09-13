# Rekordbox Serato Bridge

This independent ShadowRoom Music plugin (a Shadow Producers tool) provides bidirectional Rekordbox and Serato library inspection and conversion previews. The first release is read-only: it never edits a vendor database or an audio file.

It reads Rekordbox 6/7 through `pyrekordbox` and Serato's `master.sqlite` in read-only mode, normalizes tracks and Cue points, matches local assets, and reports which fields can be migrated in either direction. A future write release will use staging copies, explicit confirmation, backups, and a read-back verification pass.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

## Preview either direction

```sh
.venv/bin/shadow-rb-serato preview-rb-to-serato \
  --rekordbox-database "$HOME/Library/Pioneer/rekordbox/master.db" \
  --rekordbox-dir "$HOME/Library/Pioneer/rekordbox" \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" \
  --output preview.json

.venv/bin/shadow-rb-serato preview-serato-to-rb \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" \
  --rekordbox-database "$HOME/Library/Pioneer/rekordbox/master.db" \
  --rekordbox-dir "$HOME/Library/Pioneer/rekordbox" \
  --output reverse-preview.json
```

The report distinguishes exact path, filename/size, and metadata matches; unmatched tracks; streaming sources; duration conflicts; Cue counts; and target assets not referenced by the source library.

## MCP

`.mcp.json` exposes both read-only preview operations over stdio. The server has no write tool. It accepts absolute paths in tool arguments so a future host can apply its own permission and confirmation policy.

## License

MIT for this plugin. `pyrekordbox` is MIT. Any optional parser or audio-analysis dependency must be reviewed separately before redistribution.
