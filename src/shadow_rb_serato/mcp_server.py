from __future__ import annotations

import json
import sys

from .model import to_json
from .preview import preview
from .readers import read_rekordbox, read_serato


TOOLS = {
    "preview_rekordbox_to_serato": "Create a read-only Rekordbox to Serato conversion preview.",
    "preview_serato_to_rekordbox": "Create a read-only Serato to Rekordbox conversion preview.",
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
        return response(request_id, {"tools": [{"name": name, "description": description, "inputSchema": _schema()} for name, description in TOOLS.items()]})
    if method != "tools/call":
        return response(request_id, error=f"Unsupported method: {method}")
    name = params.get("name")
    arguments = params.get("arguments") or {}
    try:
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
