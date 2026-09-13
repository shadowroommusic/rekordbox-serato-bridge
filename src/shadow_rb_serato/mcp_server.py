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
        return response(request_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "rekordbox-serato-bridge", "version": "0.1.0"}})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return response(request_id, {"tools": [{"name": name, "description": description, "inputSchema": _schema()} for name, description in TOOLS.items()]})
    if method != "tools/call":
        return response(request_id, error=f"Unsupported method: {method}")
    name = params.get("name")
    try:
        rb = read_rekordbox(params["arguments"]["rekordbox_database"], params["arguments"]["rekordbox_dir"])
        serato = read_serato(params["arguments"]["serato_database"])
        if name == "preview_rekordbox_to_serato":
            value = preview("rekordbox", rb, "serato", serato)
        elif name == "preview_serato_to_rekordbox":
            value = preview("serato", serato, "rekordbox", rb)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return response(request_id, {"content": [{"type": "text", "text": json.dumps(to_json(value), ensure_ascii=False)}]})
    except Exception as exc:
        return response(request_id, error=exc)


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
