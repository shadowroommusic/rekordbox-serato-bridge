from __future__ import annotations

import json
import sys

from .model import to_json
from .preview import preview
from .readers import read_rekordbox, read_serato
from .rekordbox_export import read_serato_sets, to_rekordbox_xml, write_rekordbox_set
from .serato_export import convert_set, read_device_tracks, read_rekordbox_sets
from .staging import stage_serato_cues


TOOLS = {
    "preview_rekordbox_to_serato": "Create a read-only Rekordbox to Serato conversion preview.",
    "preview_serato_to_rekordbox": "Create a read-only Serato to Rekordbox conversion preview.",
    "stage_serato_cues": "Write cues into a STAGING COPY of one track as Serato Markers2, then read them back to verify. The original file and both vendor databases are never touched.",
    "list_sets": "List the playlists (sets) in a local Rekordbox database.",
    "convert_set": "Convert a Rekordbox set into a Serato-ready output folder: copies with Serato Markers2 cues/loops, BeatGrid tags and a .crate playlist.",
    "convert_set_to_rekordbox": "Convert a Serato set back into Rekordbox: dry-run the database write, export a Rekordbox XML, or apply with automatic backup.",
}

SERVER_NAME = "rekordbox-serato-bridge"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"

# Methods other MCP clients probe during capability negotiation. Answering with a
# valid empty result keeps the server usable from any agent, not just Codex.
EMPTY_RESULTS = {
    "resources/list": {"resources": []},
    "resources/templates/list": {"resourceTemplates": []},
    "prompts/list": {"prompts": []},
    "logging/setLevel": {},
}


def _schema() -> dict:
    return _preview_schema()


def _preview_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "rekordbox_database": {"type": "string"},
            "rekordbox_dir": {"type": "string"},
            "serato_database": {"type": "string"},
        },
        "required": ["rekordbox_database", "rekordbox_dir", "serato_database"],
        "additionalProperties": False,
    }


def _stage_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "track": {"type": "string", "description": "Audio file to copy into staging."},
            "cues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "position_ms": {"type": "integer"},
                        "end_ms": {"type": "integer"},
                        "color": {"type": "string"},
                    },
                    "required": ["position_ms"],
                    "additionalProperties": True,
                },
            },
            "staging_dir": {"type": "string"},
        },
        "required": ["track", "cues", "staging_dir"],
        "additionalProperties": False,
    }


def _tool_schema(name: str) -> dict:
    if name == "stage_serato_cues":
        return _stage_schema()
    if name == "list_sets":
        return {
            "type": "object",
            "properties": {"rekordbox_database": {"type": "string"}, "rekordbox_dir": {"type": "string"}},
            "required": ["rekordbox_database", "rekordbox_dir"],
            "additionalProperties": False,
        }
    if name == "convert_set":
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Set name; the crate file gets this name."},
                "out_dir": {"type": "string", "description": "Output folder to copy to the USB drive root."},
                "crate_dir": {"type": "string"},
                "rekordbox_database": {"type": "string"},
                "rekordbox_dir": {"type": "string"},
                "playlist": {"type": "string", "description": "Playlist name from the local Rekordbox database."},
                "device_root": {"type": "string", "description": "rekordbox USB device root, e.g. /Volumes/f379pro."},
                "tracks": {"type": "array", "items": {"type": "string"}, "description": "Device track paths to convert."},
                "with_cues_only": {"type": "boolean", "default": False},
                "write_beatgrid": {"type": "boolean", "default": True},
            },
            "required": ["name", "out_dir"],
            "additionalProperties": False,
        }
    if name == "convert_set_to_rekordbox":
        return {
            "type": "object",
            "properties": {
                "serato_database": {"type": "string", "description": "Serato master.sqlite."},
                "set_name": {"type": "string", "description": "Serato crate name; omit to use all local tracks."},
                "name": {"type": "string", "description": "Rekordbox playlist name to create."},
                "rekordbox_database": {"type": "string"},
                "rekordbox_dir": {"type": "string"},
                "apply": {"type": "boolean", "default": False, "description": "Write to the Rekordbox database (a backup is taken first)."},
                "xml_path": {"type": "string", "description": "Write a rekordbox-compatible XML instead of touching the database."},
            },
            "required": ["serato_database"],
            "additionalProperties": False,
        }
    return _preview_schema()


def response(request_id: object, result: object = None, error: object = None) -> dict:
    value = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        value["error"] = {"code": -32000, "message": str(error)}
    else:
        value["result"] = result
    return value


def handle(message: dict) -> dict | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return response(
            request_id,
            {
                "protocolVersion": requested if isinstance(requested, str) and requested else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return response(request_id, {})
    if method in EMPTY_RESULTS:
        return response(request_id, EMPTY_RESULTS[method])
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return response(
            request_id,
            {
                "tools": [
                    {"name": name, "description": description, "inputSchema": _tool_schema(name)}
                    for name, description in TOOLS.items()
                ]
            },
        )
    if method != "tools/call":
        return response(request_id, error=f"Unsupported method: {method}")
    name = params.get("name")
    arguments = params.get("arguments") or {}
    try:
        if name == "list_sets":
            sets = read_rekordbox_sets(arguments["rekordbox_database"], arguments["rekordbox_dir"])
            value = {
                "schema_version": 1,
                "set_count": len(sets),
                "sets": [{"name": item.name, "track_count": len(item.tracks)} for item in sets],
            }
            return response(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
        if name == "convert_set_to_rekordbox":
            sets = read_serato_sets(arguments["serato_database"])
            selected = sets
            if arguments.get("set_name"):
                wanted = str(arguments["set_name"]).casefold()
                selected = [item for item in sets if item.name.casefold() == wanted] or [
                    item for item in sets if wanted in item.name.casefold()
                ]
                if not selected:
                    raise ValueError(f"Serato set not found: {arguments['set_name']!r}")
            if arguments.get("xml_path"):
                xml_path = to_rekordbox_xml(selected, arguments["xml_path"])
                value = {
                    "schema_version": 1,
                    "mode": "rekordbox-xml-export",
                    "xml": str(xml_path),
                    "sets": [{"name": item.name, "track_count": len(item.tracks)} for item in selected],
                }
            else:
                if not arguments.get("rekordbox_database"):
                    raise ValueError("convert_set_to_rekordbox needs rekordbox_database or xml_path")
                tracks = [track for item in selected for track in item.tracks]
                value = write_rekordbox_set(
                    tracks,
                    str(arguments.get("name") or selected[0].name),
                    arguments["rekordbox_database"],
                    arguments.get("rekordbox_dir") or str(Path(arguments["rekordbox_database"]).parent),
                    apply=bool(arguments.get("apply")),
                )
            return response(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
        if name == "convert_set":
            if arguments.get("device_root"):
                tracks = read_device_tracks(arguments["device_root"], paths=arguments.get("tracks") or None)
                if arguments.get("with_cues_only"):
                    tracks = [track for track in tracks if track.cues]
            elif arguments.get("rekordbox_database") and arguments.get("playlist"):
                sets = read_rekordbox_sets(arguments["rekordbox_database"], arguments["rekordbox_dir"])
                match = next((item for item in sets if item.name == arguments["playlist"]), None)
                if match is None:
                    raise ValueError(f"playlist not found: {arguments['playlist']!r}")
                tracks = match.tracks
            else:
                raise ValueError("convert_set needs either device_root or rekordbox_database + playlist")
            value = convert_set(
                tracks,
                arguments["out_dir"],
                name=str(arguments["name"]),
                crate_dir=arguments.get("crate_dir"),
                write_beatgrid=bool(arguments.get("write_beatgrid", True)),
            )
            return response(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
        if name == "stage_serato_cues":
            value = stage_serato_cues(arguments["track"], arguments["cues"], arguments["staging_dir"])
            return response(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]})
        rb = read_rekordbox(arguments["rekordbox_database"], arguments["rekordbox_dir"])
        serato = read_serato(arguments["serato_database"])
        if name == "preview_rekordbox_to_serato":
            value = preview("rekordbox", rb, "serato", serato)
        elif name == "preview_serato_to_rekordbox":
            value = preview("serato", serato, "rekordbox", rb)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return response(request_id, {"content": [{"type": "text", "text": json.dumps(to_json(value), ensure_ascii=False)}]})
    except Exception as exc:
        # Tool failures are reported inside the result (MCP `isError`), not as protocol errors.
        return response(request_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True})


def main() -> None:
    for line in sys.stdin:
        try:
            result = handle(json.loads(line))
            if result is not None:
                print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps(response(None, error=exc), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
