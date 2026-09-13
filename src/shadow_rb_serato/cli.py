from __future__ import annotations

import argparse
import json
from pathlib import Path

from .model import to_json
from .preview import preview
from .readers import read_rekordbox, read_serato
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
