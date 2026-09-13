import unittest

from shadow_rb_serato import mcp_server
from shadow_rb_serato.model import CuePoint, Track
from shadow_rb_serato.preview import preview


def make_track(source_id: str, path: str, size: int = 10, length_ms: int = 100_000) -> Track:
    return Track(
        source_id=source_id,
        title="Song",
        artist="Artist",
        path=path,
        file_name=path.rsplit("/", 1)[-1],
        size=size,
        length_ms=length_ms,
        bpm=128.0,
        key="8A",
        rating=0,
        color=0,
        cues=(CuePoint(1, 1000, None, "intro", 255, False, 0),),
    )


class PreviewTests(unittest.TestCase):
    def test_exact_path_match_preserves_cue_count(self) -> None:
        result = preview("rekordbox", [make_track("rb:1", "/music/song.mp3")], "serato", [make_track("s:1", "/music/song.mp3")])
        self.assertEqual(result["summary"]["exact_path"], 1)
        self.assertEqual(result["summary"]["matched_with_cues"], 1)
        self.assertEqual(result["matches"][0]["cue_count"], 1)

    def test_streaming_source_is_explicitly_unverifiable(self) -> None:
        source = make_track("rb:1", "apple-music:tracks:1")
        target = make_track("s:1", "streaming://1")
        object.__setattr__(target, "source_kind", "streaming")
        object.__setattr__(source, "source_kind", "streaming")
        result = preview("rekordbox", [source], "serato", [target])
        self.assertEqual(result["summary"]["unmatched"], 1)
        self.assertTrue(any("not locally verifiable" in warning for warning in result["matches"][0]["warnings"]))

    def test_duration_difference_lowers_confidence(self) -> None:
        result = preview("rekordbox", [make_track("rb:1", "/music/song.mp3")], "serato", [make_track("s:1", "/music/song.mp3", length_ms=104_000)])
        self.assertEqual(result["matches"][0]["confidence"], 0.45)
        self.assertIn("duration differs", result["matches"][0]["warnings"][0])


class McpProtocolTests(unittest.TestCase):
    """The server must answer the way any MCP client expects, not just Codex."""

    def test_ping_and_negotiation_methods_return_empty_results(self) -> None:
        for method, expected in (
            ("ping", {}),
            ("resources/list", {"resources": []}),
            ("resources/templates/list", {"resourceTemplates": []}),
            ("prompts/list", {"prompts": []}),
            ("logging/setLevel", {}),
        ):
            reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
            self.assertIsNotNone(reply)
            self.assertEqual(reply["result"], expected, method)
            self.assertNotIn("error", reply)

    def test_initialize_echoes_the_client_protocol_version(self) -> None:
        reply = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "rekordbox-serato-bridge")
        self.assertEqual(reply["result"]["capabilities"], {"tools": {}})

    def test_notifications_get_no_reply(self) -> None:
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}))

    def test_tool_failure_is_reported_with_is_error(self) -> None:
        reply = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "preview_rekordbox_to_serato",
                    "arguments": {"rekordbox_database": "/tmp/nope.db", "rekordbox_dir": "/tmp", "serato_database": "/tmp/nope.sqlite"},
                },
            }
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("FileNotFoundError", reply["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
