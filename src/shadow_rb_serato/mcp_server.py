from __future__ import annotations

import json
import sys

from .model import to_json
from .preview import preview
from .readers import read_rekordbox, read_serato
from .staging import stage_serato_cues


TOOLS = {
    "preview_rekordbox_to_serato": "Create a read-only Rekordbox to Serato conversion preview.",
    "preview_serato_to_rekordbox": "Create a read-only Serato to Rekordbox conversion preview.",
    "stage_serato_cues": "Write cues into a STAGING COPY of one track as Serato Markers2, then read them back to verify. The original file and both vendor databases are never touched.",
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
    return _stage_schema() if name == "stage_serato_cues" else _preview_schema()


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
