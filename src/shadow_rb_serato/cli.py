from __future__ import annotations

import argparse
import json
from pathlib import Path

from .model import to_json
from .preview import preview
from .readers import read_rekordbox, read_serato
from .rekordbox_export import read_serato_sets, to_rekordbox_xml, write_rekordbox_set
from .serato_export import convert_set, read_device_tracks, read_rekordbox_sets
from .staging import stage_serato_cues


def dump(value: object, output: str | None) -> None:
    text = json.dumps(to_json(value), ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main() -> int:
    parser = argparse.ArgumentParser(prog="shadow-rb-serato")
    commands = parser.add_subparsers(dest="command", required=True)
    rb = commands.add_parser("inspect-rekordbox")
    rb.add_argument("--database", required=True)
    rb.add_argument("--db-dir", required=True)
    rb.add_argument("--output")
    serato = commands.add_parser("inspect-serato")
    serato.add_argument("--database", required=True)
    serato.add_argument("--output")
    for name in ("preview-rb-to-serato", "preview-serato-to-rb"):
        item = commands.add_parser(name)
        item.add_argument("--rekordbox-database", required=True)
        item.add_argument("--rekordbox-dir", required=True)
        item.add_argument("--serato-database", required=True)
        item.add_argument("--output")
    stage = commands.add_parser("stage-cues", help="Write cues into a staging copy of one track (Serato Markers2).")
    stage.add_argument("--track", required=True, help="Audio file to copy into staging.")
    stage.add_argument("--cues", required=True, help="JSON file with cues: a list or {\"cues\": [...]}.")
    stage.add_argument("--staging-dir", required=True, help="Directory that receives the staged copy and manifest.json.")
    stage.add_argument("--output")
    list_sets = commands.add_parser("list-sets", help="List the playlists (sets) in a local Rekordbox database.")
    list_sets.add_argument("--rekordbox-database", required=True)
    list_sets.add_argument("--rekordbox-dir", required=True)
    list_sets.add_argument("--output")
    convert = commands.add_parser(
        "convert-set",
        help="Convert a Rekordbox set into a Serato-ready folder: cues, loops, beatgrid, crate.",
    )
    convert.add_argument("--name", required=True, help="Set name; the crate file gets this name.")
    convert.add_argument("--out", required=True, help="Output folder (copy it to the root of your USB drive).")
    convert.add_argument("--crate-dir", help="Where to write the .crate file (default: <out>/_Serato_/Subcrates).")
    convert.add_argument("--rekordbox-database")
    convert.add_argument("--rekordbox-dir")
    convert.add_argument("--playlist", help="Playlist name from the local Rekordbox database.")
    convert.add_argument("--device-root", help="rekordbox USB device root, e.g. /Volumes/f379pro.")
    convert.add_argument("--track", action="append", default=[], help="Device track path to convert; repeatable.")
    convert.add_argument("--with-cues-only", action="store_true", help="Device mode: only tracks that have cues.")
    convert.add_argument("--no-beatgrid", action="store_true", help="Skip writing the Serato BeatGrid tag.")
    convert.add_argument("--to", choices=["serato", "rekordbox"], default="serato", help="Conversion direction.")
    convert.add_argument("--serato-database", help="Serato master.sqlite (source for --to rekordbox).")
    convert.add_argument("--xml", help="rekordbox mode: write a rekordbox-compatible XML instead of touching the database.")
    convert.add_argument("--apply", action="store_true", help="rekordbox mode: actually write to the local Rekordbox database.")
    convert.add_argument("--output")
    args = parser.parse_args()
    if args.command == "inspect-rekordbox":
        dump(read_rekordbox(args.database, args.db_dir), args.output)
    elif args.command == "inspect-serato":
        dump(read_serato(args.database), args.output)
    elif args.command == "stage-cues":
        payload = json.loads(Path(args.cues).expanduser().read_text(encoding="utf-8"))
        cues = payload.get("cues") if isinstance(payload, dict) else payload
        if not isinstance(cues, list):
            raise SystemExit("cues JSON must be a list or an object with a 'cues' array")
        dump(stage_serato_cues(args.track, cues, args.staging_dir), args.output)
    elif args.command == "list-sets":
        sets = read_rekordbox_sets(args.rekordbox_database, args.rekordbox_dir)
        dump(
            {
                "schema_version": 1,
                "set_count": len(sets),
                "sets": [{"name": item.name, "track_count": len(item.tracks)} for item in sets],
            },
            args.output,
        )
    elif args.command == "convert-set":
        if args.to == "rekordbox":
            if not args.serato_database:
                raise SystemExit("--to rekordbox needs --serato-database")
            sets = read_serato_sets(args.serato_database)
            if args.playlist:
                wanted = args.playlist.casefold()
                selected = [item for item in sets if item.name.casefold() == wanted] or [
                    item for item in sets if wanted in item.name.casefold()
                ]
                if not selected:
                    names = ", ".join(item.name for item in sets)
                    raise SystemExit(f"Serato set not found: {args.playlist!r} (available: {names})")
            else:
                selected = [item for item in sets if item.name == "Serato 本地曲目"] or sets[:1]
            name = args.name
            if args.xml:
                xml_path = to_rekordbox_xml(selected, args.xml)
                dump(
                    {
                        "schema_version": 1,
                        "mode": "rekordbox-xml-export",
                        "xml": str(xml_path),
                        "sets": [{"name": item.name, "track_count": len(item.tracks)} for item in selected],
                        "warnings": ["Import this XML in Rekordbox (File → Import), or keep it as a portable backup."],
                    },
                    args.output,
                )
                return 0
            if not args.rekordbox_database:
                raise SystemExit("--to rekordbox needs --rekordbox-database (or use --xml)")
            tracks = [track for item in selected for track in item.tracks]
            dump(
                write_rekordbox_set(
                    tracks,
                    name,
                    args.rekordbox_database,
                    args.rekordbox_dir or Path(args.rekordbox_database).parent,
                    apply=args.apply,
                ),
                args.output,
            )
            return 0
        if args.device_root:
            tracks = read_device_tracks(args.device_root, paths=args.track or None)
            if args.with_cues_only:
                tracks = [track for track in tracks if track.cues]
        elif args.rekordbox_database and args.playlist:
            sets = read_rekordbox_sets(args.rekordbox_database, args.rekordbox_dir or Path(args.rekordbox_database).parent)
            match = next((item for item in sets if item.name == args.playlist), None)
            if match is None:
                raise SystemExit(f"playlist not found: {args.playlist!r} (use list-sets to see the names)")
            tracks = match.tracks
        else:
            raise SystemExit("convert-set needs either --device-root or --rekordbox-database with --playlist")
        if not tracks:
            raise SystemExit("nothing to convert: no tracks matched")
        dump(
            convert_set(tracks, args.out, name=args.name, crate_dir=args.crate_dir, write_beatgrid=not args.no_beatgrid),
            args.output,
        )
    else:
        rb_tracks = read_rekordbox(args.rekordbox_database, args.rekordbox_dir)
        serato_tracks = read_serato(args.serato_database)
        result = preview(
            "rekordbox" if args.command == "preview-rb-to-serato" else "serato",
            rb_tracks if args.command == "preview-rb-to-serato" else serato_tracks,
            "serato" if args.command == "preview-rb-to-serato" else "rekordbox",
            serato_tracks if args.command == "preview-rb-to-serato" else rb_tracks,
        )
        dump(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
