import base64
import unittest
import json
import shutil
import sqlite3
import struct
import tempfile
from pathlib import Path

from shadow_rb_serato import mcp_server
from shadow_rb_serato.colors import (
    REKORDBOX_PALETTE,
    SERATO_DEFAULT_RGB,
    color_table_index_for_rgb,
    rgb_from_color_table_index,
)
from shadow_rb_serato.model import CuePoint, Track
from shadow_rb_serato.preview import preview
from shadow_rb_serato.readers import read_serato
from shadow_rb_serato.readers import open_serato_library
from shadow_rb_serato.rekordbox_export import (
    FIRST_PAD_KIND,
    LAST_PAD_KIND,
    MEMORY_CUE_KIND,
    SeratoSet,
    _beat_loop_size,
    cue_kind_for_index,
    read_serato_sets,
    to_rekordbox_xml,
)
from shadow_rb_serato.serato_export import SetTrack, build_beatgrid, convert_set, cue_to_marker, markers_for
from shadow_rb_serato.serato_markers import (
    BEATGRID_DESC,
    FLAC_BEATGRID_KEY,
    FLAC_MARKERS2_KEY,
    FLAC_MARKERS2_PREFIX,
    MARKERS1_VERSION,
    MARKERS2_DESC,
    SeratoMarker,
    build_markers2,
    container_of,
    encode_bytes32,
    extract_id3_tag,
    from_cue_points,
    parse_beatgrid_bpm,
    parse_beatgrid_anchor,
    parse_geob_tags,
    parse_markers1,
    parse_markers2,
    read_beatgrid_bpm,
    read_flac_comments,
    read_markers,
    replace_geob_tags,
)
from shadow_rb_serato.staging import stage_serato_cues


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


def build_markers1_blob(cues) -> bytes:
    """按规范手工构造一个 Serato Markers_ 标签（v1 只读，不提供写入）。"""
    data = bytearray(MARKERS1_VERSION) + struct.pack(">I", len(cues))
    for start, end, entry_type in cues:
        def encode(value):
            return b"\x7f\x7f\x7f\x7f" if value is None else encode_bytes32(struct.pack(">I", value)[1:])

        data += struct.pack(
            ">B4sB4s6s4sBB",
            0x00 if start is not None else 0x7F,
            encode(start),
            0x00 if end is not None else 0x7F,
            encode(end),
            b"\x00" * 6,
            encode_bytes32(bytes.fromhex("CCCCCC")),
            entry_type,
            0,
        )
    data += b"\x00\x00\x00\x00"
    return bytes(data)


def write_fake_mp3(path: Path) -> Path:
    """一个只带最小 MPEG 帧头的假文件，足够承载 ID3 标签测试。"""
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)
    return path


def extended80(value: float) -> bytes:
    import math

    exponent = math.floor(math.log2(value))
    mantissa = int(value / (2 ** exponent) * (1 << 63))
    return struct.pack(">H", 16383 + exponent) + mantissa.to_bytes(8, "big")


def write_fake_aiff(path: Path) -> Path:
    def chunk(chunk_id: bytes, body: bytes) -> bytes:
        padding = b"\x00" if len(body) % 2 else b""
        return chunk_id + struct.pack(">I", len(body)) + body + padding

    comm = struct.pack(">hIh", 1, 64, 16) + extended80(44100.0)
    ssnd = struct.pack(">II", 0, 0) + b"\x00" * 128
    body = b"AIFF" + chunk(b"COMM", comm) + chunk(b"SSND", ssnd)
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
    return path


def write_fake_wav_with_id3(path: Path, tag: bytes) -> Path:
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 2, 16)
    samples = b"\x00" * 128
    body = (
        b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(samples)) + samples
        + b"id3 " + struct.pack("<I", len(tag)) + tag
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def write_fake_flac(path: Path, audio: bytes = b"AUDIO" * 16) -> Path:
    """最小 FLAC：STREAMINFO + 空的 Vorbis comment + 假音频数据。"""
    streaminfo = struct.pack(">I", 0) + b"\x00" * 30

    def block(block_type: int, body: bytes, last: bool) -> bytes:
        header = (0x80 if last else 0x00) | block_type
        return bytes([header]) + len(body).to_bytes(3, "big") + body

    vendor = b"reference libFLAC 1.3.2"
    comment = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    data = block(0, streaminfo, False) + block(4, comment, True) + audio
    path.write_bytes(b"fLaC" + data)
    return path


class SeratoMarkerTests(unittest.TestCase):
    def test_markers2_round_trip(self) -> None:
        markers = [
            SeratoMarker("cue", 0, 12345, None, "intro", 0xCC0000),
            SeratoMarker("cue", 1, 96050, None, None, 0x0000CC),
            SeratoMarker("loop", 2, 30000, 60000, "drop", 0x00CC00, False),
        ]
        parsed = parse_markers2(build_markers2(markers))
        self.assertEqual(
            [(m.kind, m.position_ms, m.end_ms, m.name) for m in parsed],
            [("cue", 12345, None, "intro"), ("cue", 96050, None, None), ("loop", 30000, 60000, "drop")],
        )
        self.assertEqual(parsed[0].color, 0xCC0000)

    def test_markers2_loop_layout_matches_serato_bytes(self) -> None:
        # Serato 的 LOOP 条目是固定 20 字节头 + label + \0，并且带 FF FF FF FF 魔数
        # （对齐 Mixxx 的 SeratoMarkers2LoopEntry::dump）：
        #   00 | index | start(4) | end(4) | FF*4 | 00 | R | G | B | 00 | locked
        blob = build_markers2([SeratoMarker("loop", 0, 61364, 63079, None, None, False)])
        encoded = blob[2:].split(b"\x00", 1)[0].replace(b"\n", b"").replace(b"\r", b"")
        padding = b"A==" if len(encoded) % 4 == 1 else b"=" * (-len(encoded) % 4)
        payload = base64.b64decode(encoded + padding)
        self.assertEqual(payload[:2], b"\x01\x01")
        body = payload[2:]
        self.assertTrue(body.startswith(b"LOOP\x00"))
        length = struct.unpack(">I", body[5:9])[0]
        entry = body[9 : 9 + length]
        self.assertEqual(entry[0:2], b"\x00\x00")  # unknown 0 + index 0
        self.assertEqual(struct.unpack(">II", entry[2:10]), (61364, 63079))
        self.assertEqual(entry[10:14], b"\xff\xff\xff\xff")
        self.assertEqual(entry[14], 0x00)
        self.assertEqual(entry[15:18], bytes((0x27, 0xAA, 0xE1)))  # Serato 固定 loop 颜色
        self.assertEqual(entry[18], 0x00)
        self.assertEqual(entry[19], 0)  # locked = False
        self.assertEqual(entry[20:], b"\x00")  # 空 label 的终止符
        self.assertEqual(len(entry), 21)

    def test_beatgrid_bpm_is_read_back(self) -> None:
        # Serato 库里 BPM 列经常是空的，所以要从文件的 Serato BeatGrid 标签里兜底读 BPM。
        blob = build_beatgrid(140.0, 0)
        self.assertAlmostEqual(parse_beatgrid_bpm(blob), 140.0, places=3)
        # 非 terminal marker 也要能跳过（这里 2 个 marker：1 个非 terminal + terminal）
        two_markers = (
            b"\x01\x00"
            + struct.pack(">I", 2)
            + struct.pack(">fI", 1.0, 4)
            + struct.pack(">ff", 2.0, 128.0)
            + b"\x00"
        )
        self.assertAlmostEqual(parse_beatgrid_bpm(two_markers), 128.0, places=3)
        self.assertEqual(parse_beatgrid_anchor(two_markers), 2000)

    def test_convert_set_uses_rekordbox_beat_anchor(self) -> None:
        # BeatGrid 的锚点优先用 rekordbox 分析出来的第一拍，没有分析数据时才退回第一条 cue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_fake_mp3(root / "track.mp3")
            cues = (CuePoint(1, 9000, None, "A", None, None, None),)

            with_anchor = SetTrack(
                id="rb:1", title="t", artist="", path=str(source), bpm=128.0, cues=cues, beat_anchor_ms=25
            )
            report = convert_set([with_anchor], root / "out", name="t")
            staged = Path(report["tracks"][0]["staged"])
            payload = parse_geob_tags(extract_id3_tag(staged))[BEATGRID_DESC]
            self.assertEqual(parse_beatgrid_anchor(payload), 25)
            self.assertAlmostEqual(parse_beatgrid_bpm(payload), 128.0, places=3)

            without_anchor = SetTrack(id="rb:2", title="t", artist="", path=str(source), bpm=128.0, cues=cues)
            report2 = convert_set([without_anchor], root / "out2", name="t")
            staged2 = Path(report2["tracks"][0]["staged"])
            payload2 = parse_geob_tags(extract_id3_tag(staged2))[BEATGRID_DESC]
            self.assertEqual(parse_beatgrid_anchor(payload2), 9000)

    def test_beat_anchor_helper_handles_missing_analysis(self) -> None:
        from shadow_rb_serato.readers import beat_anchor_from_analysis

        self.assertIsNone(beat_anchor_from_analysis("/tmp/does-not-exist", ""))
        self.assertIsNone(beat_anchor_from_analysis("/tmp/does-not-exist", "/PIONEER/USBANLZ/x/ANLZ0000.DAT"))

    def test_markers1_spec_blob_is_read(self) -> None:
        parsed = parse_markers1(build_markers1_blob([(45000, None, 1), (300000, 360000, 3)]))
        self.assertEqual(parsed[0].kind, "cue")
        self.assertEqual(parsed[0].position_ms, 45000)
        self.assertEqual(parsed[1].kind, "loop")
        self.assertEqual((parsed[1].position_ms, parsed[1].end_ms), (300000, 360000))

    def test_rejects_unknown_versions(self) -> None:
        with self.assertRaises(ValueError):
            parse_markers2(b"\x09\x09\x00")
        with self.assertRaises(ValueError):
            parse_markers1(b"\x09\x09\x00\x00\x00\x00")

    def test_cue_points_round_trip(self) -> None:
        cues = [
            {"name": "A", "position_ms": 1000, "color": "#CC0000"},
            {"name": "loop", "position_ms": 2000, "end_ms": 4000},
            CuePoint(1, 5000, None, "plain", 0x0000FF, False, None),
        ]
        markers = from_cue_points(cues)
        self.assertEqual([m.kind for m in markers], ["cue", "loop", "cue"])
        self.assertEqual(markers[0].color, 0xCC0000)
        parsed = parse_markers2(build_markers2(markers))
        self.assertEqual((parsed[1].position_ms, parsed[1].end_ms), (2000, 4000))

    def test_file_level_round_trip_preserves_audio_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_fake_mp3(Path(tmp) / "track.mp3")
            audio = path.read_bytes()
            replace_geob_tags(path, {MARKERS2_DESC: build_markers2([SeratoMarker("cue", 0, 7777, None, "x", 0x00FF00)])})
            markers, source = read_markers(path)
            self.assertEqual(source, MARKERS2_DESC)
            self.assertEqual([(m.kind, m.position_ms, m.name) for m in markers], [("cue", 7777, "x")])
            self.assertTrue(path.read_bytes().endswith(audio))

    def test_aiff_tags_are_written_as_an_id3_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_fake_aiff(Path(tmp) / "track.aiff")
            self.assertEqual(container_of(path), "aiff")
            replace_geob_tags(path, {MARKERS2_DESC: build_markers2([SeratoMarker("cue", 0, 4321, None, "A", 0xCC0000)])})
            markers, source = read_markers(path)
            self.assertEqual(source, MARKERS2_DESC)
            self.assertEqual([m.position_ms for m in markers], [4321])
            data = path.read_bytes()
            self.assertEqual(data[:4], b"FORM")
            self.assertEqual(data[8:12], b"AIFF")
            self.assertEqual(struct.unpack(">I", data[4:8])[0], len(data) - 8)
            self.assertIn(b"COMM", data)

    def test_wav_id3_chunk_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_fake_mp3(root / "template.mp3")
            replace_geob_tags(source, {MARKERS2_DESC: build_markers2([SeratoMarker("cue", 0, 999, None, "W", 0x00FF00)])})
            tag = extract_id3_tag(source)
            wav = write_fake_wav_with_id3(root / "track.wav", tag)
            self.assertEqual(container_of(wav), "wav")
            markers, source_label = read_markers(wav)
            self.assertEqual(source_label, MARKERS2_DESC)
            self.assertEqual([(m.position_ms, m.name) for m in markers], [(999, "W")])

    def test_flac_markers_and_beatgrid_round_trip(self) -> None:
        # FLAC 的 Serato 数据在 Vorbis comment 里（SERATO_MARKERS_V2 / SERATO_BEATGRID），
        # 格式对齐 Mixxx：Markers2 是「前缀 + 0x0101 + base64(内容)」再整体 base64 一次。
        with tempfile.TemporaryDirectory() as tmp:
            path = write_fake_flac(Path(tmp) / "track.flac")
            markers = [
                SeratoMarker("cue", 0, 1234, None, "A", 0xD33D34),
                SeratoMarker("loop", 0, 5000, 7000, None, None, False),
            ]
            replace_geob_tags(
                path,
                {MARKERS2_DESC: build_markers2(markers), BEATGRID_DESC: build_beatgrid(140.0, 500)},
            )
            read_back, source = read_markers(path)
            self.assertEqual(container_of(path), "flac")
            self.assertEqual(source, MARKERS2_DESC)
            self.assertEqual(
                [(m.kind, m.position_ms, m.end_ms, m.name) for m in read_back],
                [("cue", 1234, None, "A"), ("loop", 5000, 7000, None)],
            )
            self.assertEqual(read_back[0].color, 0xD33D34)
            self.assertAlmostEqual(read_beatgrid_bpm(path), 140.0, places=3)
            # 音频数据与其它元数据块都还在
            self.assertTrue(path.read_bytes().endswith(b"AUDIO" * 16))
            comments = read_flac_comments(path)
            self.assertIn(FLAC_MARKERS2_KEY, comments)
            self.assertIn(FLAC_BEATGRID_KEY, comments)
            decoded = base64.b64decode(comments[FLAC_MARKERS2_KEY])
            self.assertTrue(decoded.startswith(FLAC_MARKERS2_PREFIX))
            # 再写一次不应破坏结构（Vorbis comment 键不会重复堆积）
            replace_geob_tags(path, {MARKERS2_DESC: build_markers2(markers)})
            self.assertEqual(len(read_markers(path)[0]), 2)

    def test_flac_without_serato_tags_reports_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_fake_flac(Path(tmp) / "plain.flac")
            markers, source = read_markers(path)
            self.assertEqual(markers, [])
            self.assertEqual(source, "no-serato-markers")
            self.assertIsNone(read_beatgrid_bpm(path))

    def test_unsupported_containers_are_refused_instead_of_corrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "track.ogg"
            path.write_bytes(b"OggS" + b"\x00" * 64)
            self.assertEqual(container_of(path), "ogg")
            self.assertEqual(read_markers(path)[1], "ogg-unsupported")
            with self.assertRaises(ValueError):
                replace_geob_tags(path, {MARKERS2_DESC: build_markers2([])})

    def test_existing_id3v23_tag_is_preserved_when_adding_markers(self) -> None:
        def synchsafe(value: int) -> bytes:
            return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))

        def frame(frame_id: bytes, body: bytes) -> bytes:
            return frame_id + struct.pack(">I", len(body)) + b"\x00\x00" + body

        title = "Existing Title".encode("latin-1")
        artwork = b"\x00" * 3000
        body = frame(b"TIT2", b"\x00" + title) + frame(b"APIC", b"\x00image/jpeg\x00\x03cover\x00" + artwork)
        tag = b"ID3\x03\x00\x00" + synchsafe(len(body)) + body
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "track.mp3"
            path.write_bytes(tag + b"\xff\xfb\x90\x00" + b"\x00" * 256)
            replace_geob_tags(path, {MARKERS2_DESC: build_markers2([SeratoMarker("cue", 0, 3333, None, "A", 0xCC0000)])})
            markers, source = read_markers(path)
            self.assertEqual(source, MARKERS2_DESC)
            self.assertEqual([(m.position_ms, m.name) for m in markers], [(3333, "A")])
            blob = path.read_bytes()
            self.assertEqual(blob[:5], b"ID3\x03\x00")
            self.assertIn(b"TIT2", blob)
            self.assertIn(title, blob)
            self.assertIn(b"APIC", blob)
            self.assertTrue(blob.endswith(b"\xff\xfb\x90\x00" + b"\x00" * 256))


class StagingTests(unittest.TestCase):
    def test_stage_cues_writes_only_the_copy_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_fake_mp3(root / "track.mp3")
            before = source.read_bytes()
            staging = root / "staging"
            report = stage_serato_cues(source, [{"name": "A", "position_ms": 1500}, {"position_ms": 9000, "end_ms": 12000, "name": "loop"}], staging)

            self.assertEqual(report["status"], "verified", report)
            self.assertTrue(report["verification"]["matches"])
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(Path(report["staged"]).is_file())
            manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["entry_count"], 1)
            self.assertEqual(manifest["entries"][0]["status"], "verified")
            staged_markers, _ = read_markers(report["staged"])
            self.assertEqual([m.position_ms for m in staged_markers], [1500, 9000])

    def test_second_stage_run_uses_a_separate_file_and_appends_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_fake_mp3(root / "track.mp3")
            staging = root / "staging"
            first = stage_serato_cues(source, [{"position_ms": 100}], staging)
            second = stage_serato_cues(source, [{"position_ms": 200}], staging)
            self.assertNotEqual(first["staged"], second["staged"])
            manifest = json.loads(Path(second["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["entry_count"], 2)

    def test_invalid_cues_are_rejected_before_any_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_fake_mp3(root / "track.mp3")
            staging = root / "staging"
            with self.assertRaises(ValueError):
                stage_serato_cues(source, [], staging)
            with self.assertRaises(ValueError):
                stage_serato_cues(source, [{"position_ms": 1000, "end_ms": None, "kind": "loop"}], staging)
            self.assertFalse(staging.exists())

    def test_missing_source_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                stage_serato_cues(Path(tmp) / "nope.mp3", [{"position_ms": 1}], Path(tmp) / "staging")


class SeratoReaderTests(unittest.TestCase):
    def build_database(self, root: Path, mp3: Path) -> Path:
        database = root / "master.sqlite"
        con = sqlite3.connect(str(database))
        con.execute(
            "CREATE TABLE asset (id INTEGER PRIMARY KEY, file_name TEXT, portable_id TEXT, file_size INTEGER, "
            "name TEXT, artist TEXT, bpm REAL, key TEXT, rating INTEGER, color INTEGER, length_ms INTEGER)"
        )
        con.execute(
            "INSERT INTO asset VALUES (1, ?, ?, ?, 'Local Track', 'Artist', 128.0, '8A', 0, 0, 180000)",
            (mp3.name, str(mp3).lstrip("/"), mp3.stat().st_size),
        )
        con.execute(
            "INSERT INTO asset VALUES (2, 'streaming://apple_music/1', NULL, 0, 'Stream', 'Artist', 0, '', NULL, NULL, 200000)"
        )
        con.commit()
        con.close()
        return database

    def test_local_assets_get_serato_markers_and_streaming_ones_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp3 = write_fake_mp3(root / "track.mp3")
            replace_geob_tags(mp3, {MARKERS2_DESC: build_markers2([SeratoMarker("cue", 0, 4200, None, "A", 0xCC0000)])})
            database = self.build_database(root, mp3)
            warnings: "list[str]" = []
            tracks = read_serato(database, warnings=warnings)
            local, streaming = tracks[0], tracks[1]
            self.assertEqual(local.path, str(mp3))
            self.assertEqual(local.source_kind, "local")
            self.assertEqual([(cue.in_ms, cue.comment) for cue in local.cues], [(4200, "A")])
            self.assertEqual(streaming.source_kind, "streaming")
            self.assertEqual(streaming.cues, ())
            self.assertEqual(warnings, [])

    def test_missing_local_file_is_reported_as_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp3 = write_fake_mp3(root / "track.mp3")
            database = self.build_database(root, mp3)
            mp3.unlink()
            warnings: "list[str]" = []
            tracks = read_serato(database, warnings=warnings)
            self.assertEqual(tracks[0].cues, ())
            self.assertTrue(any("not found on disk" in warning for warning in warnings))


class PreviewCueTests(unittest.TestCase):
    def test_cue_count_mismatch_is_reported(self) -> None:
        source = make_track("rb:1", "/music/song.mp3")
        target = make_track("s:1", "/music/song.mp3")
        object.__setattr__(target, "cues", (CuePoint(1, 1000, None, "a", 0, False, 0), CuePoint(1, 2000, None, "b", 0, False, 0)))
        result = preview("rekordbox", [source], "serato", [target])
        match = result["matches"][0]
        self.assertEqual(match["cue_count"], 1)
        self.assertEqual(match["target_cue_count"], 2)
        self.assertTrue(any("while the source has" in warning for warning in match["warnings"]))
        self.assertEqual(result["target"]["local_assets_with_cues"], 1)
        self.assertEqual(result["target"]["cue_count"], 2)


class SetExportTests(unittest.TestCase):
    def test_cue_mapping_covers_memory_hot_and_loop(self) -> None:
        markers = markers_for(
            SetTrack(
                id="rb:1",
                title="Track",
                artist="Artist",
                path="/tmp/track.mp3",
                bpm=128.0,
                cues=(
                    CuePoint(0, 1000, None, "memory", 0xCC0000, False, None),
                    CuePoint(1, 2000, None, "A", 0xCC8800, False, 0),
                    CuePoint(1, 3000, 3400, "loop", 0x00CC00, True, 400),
                ),
            )
        )
        self.assertEqual([marker.kind for marker in markers], ["cue", "cue", "loop"])
        self.assertEqual([marker.position_ms for marker in markers], [1000, 2000, 3000])
        self.assertEqual(markers[2].end_ms, 3400)
        self.assertEqual(cue_to_marker(0, CuePoint(1, 500, None, None, None, False, None)).kind, "cue")

    def test_beatgrid_layout(self) -> None:
        blob = build_beatgrid(128.0, 1500)
        self.assertEqual(blob[:2], b"\x01\x00")
        self.assertEqual(struct.unpack(">I", blob[2:6])[0], 1)
        self.assertAlmostEqual(struct.unpack(">f", blob[6:10])[0], 1.5, places=4)
        self.assertAlmostEqual(struct.unpack(">f", blob[10:14])[0], 128.0, places=4)
        self.assertEqual(blob[14:], b"\x00")
        with self.assertRaises(ValueError):
            build_beatgrid(0)

    def test_convert_set_writes_tags_crate_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_fake_mp3(root / "track.mp3")
            before = source.read_bytes()
            track = SetTrack(
                id="rb:1",
                title="Track",
                artist="Artist",
                path=str(source),
                bpm=130.0,
                cues=(CuePoint(1, 27, None, "A", 0xCC0000, False, 0), CuePoint(1, 20335, None, "B", 0xCC8800, False, 0)),
            )
            out = root / "usb"
            report = convert_set([track], out, name="测试")

            self.assertEqual(report["converted"], 1)
            self.assertEqual(report["missing"], 0)
            staged = Path(report["tracks"][0]["staged"])
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.parent, out / "ShadowRoom" / "测试")
            markers, marker_source = read_markers(staged)
            self.assertEqual(marker_source, MARKERS2_DESC)
            self.assertEqual([(m.position_ms, m.name) for m in markers], [(27, "A"), (20335, "B")])
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(report["tracks"][0]["source_unchanged"])

            crate = Path(report["crate"])
            self.assertTrue(crate.is_file())
            self.assertEqual(crate.parent, out / "_Serato_" / "Subcrates")
            blob = crate.read_bytes()
            self.assertEqual(blob[:4], b"vrsn")
            self.assertIn(b"ptrk", blob)
            self.assertIn(str(Path("ShadowRoom") / "测试" / "track.mp3").encode("utf-16-be"), blob)

            manifest = Path(report["manifest"])
            self.assertTrue(manifest.is_file())
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["set"], "测试")

    def test_convert_set_reports_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = convert_set(
                [SetTrack(id="rb:1", title="Gone", artist="", path="/tmp/definitely-missing.mp3")],
                Path(tmp) / "usb",
                name="测试",
            )
            self.assertEqual(report["converted"], 0)
            self.assertEqual(report["missing"], 1)
            self.assertIsNone(report["crate"])


class RekordboxExportTests(unittest.TestCase):
    def test_rekordbox_palette_maps_both_ways(self) -> None:
        # 实测表（2026-09-14，rekordbox GUI 逐格设色后读 ColorTableIndex）
        self.assertEqual(len(REKORDBOX_PALETTE), 16)
        self.assertEqual(rgb_from_color_table_index(42), 0xD33D34)  # 红
        self.assertEqual(rgb_from_color_table_index(1), 0x3A59F6)  # 蓝
        self.assertEqual(rgb_from_color_table_index(62), 0x6773F6)  # 蓝紫（编号最大）
        self.assertIsNone(rgb_from_color_table_index(0))  # 不设颜色
        self.assertIsNone(rgb_from_color_table_index(None))
        self.assertIsNone(rgb_from_color_table_index(99))  # 未知编号

        # Serato RGB → 最近的 rekordbox 编号
        self.assertEqual(color_table_index_for_rgb(0xD33D34), 42)
        self.assertEqual(color_table_index_for_rgb(0xFF0000), 42)  # 纯红也落到红
        self.assertEqual(color_table_index_for_rgb(0x3A59F6), 1)
        # 白色/黑色 = 没颜色（我们给没颜色的 cue 写的就是白色）
        self.assertEqual(color_table_index_for_rgb(SERATO_DEFAULT_RGB), 0)
        self.assertEqual(color_table_index_for_rgb(0x000000), 0)
        self.assertEqual(color_table_index_for_rgb(None), 0)

    def test_cue_color_becomes_serato_marker_color(self) -> None:
        # rekordbox 侧读到调色板编号后，导出 Serato 时写成对应 RGB
        track = SetTrack(
            id="rb:1",
            title="t",
            artist="",
            path="/tmp/t.mp3",
            cues=(
                CuePoint(1, 1000, None, "A", rgb_from_color_table_index(42), None, None),
                CuePoint(2, 2000, None, "B", None, None, None),
            ),
        )
        markers = markers_for(track)
        self.assertEqual(markers[0].color, 0xD33D34)
        self.assertEqual(markers[1].color, SERATO_DEFAULT_RGB)

    def test_cue_kind_mapping_matches_rekordbox_pad_order(self) -> None:
        # 实机实测（2026-09-14，16 条对照 cue 逐 pad 核对）：pad A..P 的 Kind 是
        # (1,2,3,5,6,…,17)；Kind=4 在 rekordbox 界面里不显示，必须跳过。
        self.assertEqual(cue_kind_for_index(0), FIRST_PAD_KIND)
        self.assertEqual(cue_kind_for_index(1), 2)
        self.assertEqual(cue_kind_for_index(2), 3)
        self.assertEqual(cue_kind_for_index(3), 5)  # pad D 是 5，不是 4
        self.assertEqual(cue_kind_for_index(4), 6)
        self.assertEqual(cue_kind_for_index(15), LAST_PAD_KIND)
        self.assertNotIn(4, [cue_kind_for_index(index) for index in range(16)])
        # 超过 16 条没有更多 pad，降级为 memory cue 但保留数据。
        self.assertEqual(cue_kind_for_index(16), MEMORY_CUE_KIND)
        self.assertEqual(cue_kind_for_index(20), MEMORY_CUE_KIND)

    def test_serato_marker_indices_keep_loops_separate(self) -> None:
        # Serato 的 hot cue / saved loop 是两套独立槽位：loop 不能占 cue 的序号，
        # 否则 loop 后面的 cue 会在 Serato 里串位（实机确认 index=0/1 → cue 1/2）。
        track = SetTrack(
            id="rb:1",
            title="t",
            artist="",
            path="/tmp/t.mp3",
            cues=(
                CuePoint(1, 1000, None, "A", None, None, None),
                CuePoint(2, 2000, None, "B", None, None, None),
                CuePoint(3, 3000, 4000, None, None, False, None),
                CuePoint(5, 5000, None, None, None, None, None),
            ),
        )
        markers = markers_for(track)
        self.assertEqual(
            [(m.kind, m.index, m.position_ms) for m in markers],
            [("cue", 0, 1000), ("cue", 1, 2000), ("loop", 0, 3000), ("cue", 2, 5000)],
        )

    def test_beat_loop_size_packs_beats_like_rekordbox(self) -> None:
        track = SetTrack(id="rb:1", title="CONTEXT", artist="", path="/tmp/track.mp3", bpm=140.0)
        plan = {"is_loop": True, "in_ms": 61364, "out_ms": 63079}
        # 1715 ms @140bpm = 4.001 拍 → (4 << 16) | 1 = 262145（与 rekordbox 实测值一致）
        self.assertEqual(_beat_loop_size(track, plan), 262145)
        self.assertEqual(_beat_loop_size(track, {"is_loop": False, "in_ms": 0, "out_ms": None}), 0)
        # 16 拍（实机值 1048577）
        self.assertEqual(
            _beat_loop_size(track, {"is_loop": True, "in_ms": 0, "out_ms": 6857}), 1048577
        )
        # 不对拍的 loop（Serato 里随手拉的 1.5s）写 0 → rekordbox 按任意长度显示
        self.assertEqual(_beat_loop_size(track, {"is_loop": True, "in_ms": 0, "out_ms": 1500}), 0)

    def build_serato_database(self, root: Path, mp3: Path) -> Path:
        database = root / "master.sqlite"
        connection = sqlite3.connect(str(database))
        connection.execute(
            "CREATE TABLE asset (id INTEGER PRIMARY KEY, file_name TEXT, portable_id TEXT, name TEXT, artist TEXT, bpm REAL, key TEXT, length_ms INTEGER)"
        )
        connection.execute(
            "CREATE TABLE container (id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT, type INTEGER)"
        )
        connection.execute(
            "CREATE TABLE container_asset (id INTEGER PRIMARY KEY, asset_id INTEGER, location_container_id INTEGER, list_order INTEGER)"
        )
        connection.execute(
            "INSERT INTO asset VALUES (1, ?, ?, 'CONTEXT', '33 Below', 140.0, 'Cm', 180000)",
            (mp3.name, str(mp3).lstrip("/")),
        )
        connection.execute("INSERT INTO container VALUES (5, 0, 'Serato Library root', 0)")
        connection.execute("INSERT INTO container VALUES (77, 5, '测试', 1)")
        connection.execute("INSERT INTO container_asset VALUES (900, 1, 77, 1)")
        connection.commit()
        connection.close()
        return database

    def test_read_serato_sets_includes_crate_and_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp3 = write_fake_mp3(root / "context.mp3")
            replace_geob_tags(
                mp3,
                {MARKERS2_DESC: build_markers2([SeratoMarker("cue", 0, 79, None, "A", 0xCC0000), SeratoMarker("cue", 1, 27079, None, "B", 0xCC8800)])},
            )
            database = self.build_serato_database(root, mp3)
            sets = read_serato_sets(database)
            crate = next(item for item in sets if item.name == "测试")
            self.assertEqual(len(crate.tracks), 1)
            track = crate.tracks[0]
            self.assertEqual(track.title, "CONTEXT")
            self.assertEqual([(cue.in_ms, cue.comment) for cue in track.cues], [(79, "A"), (27079, "B")])
            self.assertEqual(track.bpm, 140.0)
            self.assertTrue(any(item.name == "Serato 本地曲目" for item in sets))

    def test_rekordbox_xml_contains_cues_loops_and_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp3 = write_fake_mp3(root / "track.mp3")
            track = SetTrack(
                id="serato:1",
                title="Track",
                artist="Artist",
                path=str(mp3),
                bpm=128.0,
                key="8A",
                duration_ms=240000,
                cues=(
                    CuePoint(1, 27, None, "A", 0xCC0000, False, 0),
                    CuePoint(1, 3000, 3400, "loop", 0x00CC00, True, 400),
                ),
            )
            xml_path = to_rekordbox_xml([SeratoSet("测试", [track])], root / "rekordbox.xml")
            text = xml_path.read_text(encoding="utf-8")
            self.assertIn('<DJ_PLAYLISTS Version="1.0.0">', text)
            self.assertIn('Name="测试"', text)
            self.assertIn('Location="file://localhost', text)
            self.assertIn('Type="0" Start="0.027" End="-1" Num="0"', text)
            self.assertIn('Type="4" Start="3.000" End="3.400"', text)
            self.assertIn('<TRACK Key="1" />', text)
            self.assertIn('Entries="1"', text)


class SeratoWalLibraryTests(unittest.TestCase):
    """Serato DJ Pro keeps master.sqlite in WAL mode (verified against Serato 4.x on macOS).

    A WAL database cannot be opened with `?mode=ro` while its sidecars are missing, which is exactly
    how a library looks when Serato is closed: SQLite would have to create the -shm file. The reader
    therefore falls back to a private copy of the database plus any -wal/-shm siblings.
    """

    def _wal_database(self, path: Path):
        con = sqlite3.connect(str(path))
        con.execute("pragma journal_mode=wal")
        con.execute(
            "create table asset (id integer primary key, file_name text, portable_id text, "
            "file_size integer, name text, artist text, bpm real, key text, rating integer, "
            "color text, length_ms integer)"
        )
        con.execute(
            "insert into asset (file_name, portable_id, file_size, name, artist, length_ms) "
            "values ('/x/A.aiff', '', 11, 'A', 'Artist', 1000)"
        )
        con.commit()
        return con

    def test_reads_a_wal_library_copied_without_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live.sqlite"
            writer = self._wal_database(live)
            try:
                copy_dir = root / "copied"
                copy_dir.mkdir()
                copied = copy_dir / "master.sqlite"
                shutil.copy2(live, copied)  # the -wal is intentionally left behind
                with self.assertRaises(sqlite3.OperationalError):
                    sqlite3.connect(f"file:{copied}?mode=ro", uri=True).execute("SELECT 1").fetchone()
                # The fallback opens the copy, but a library copied without its -wal has no asset
                # table left; that has to read as a clear diagnosis, not as a raw sqlite error.
                with self.assertRaises(RuntimeError) as caught:
                    read_serato(copied)
                self.assertIn("-wal", str(caught.exception))
            finally:
                writer.close()

    def test_reads_committed_rows_when_sidecars_are_copied_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live.sqlite"
            writer = self._wal_database(live)
            try:
                copy_dir = root / "copied"
                copy_dir.mkdir()
                copied = copy_dir / "master.sqlite"
                shutil.copy2(live, copied)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{live}{suffix}")
                    if sidecar.is_file():
                        shutil.copy2(sidecar, Path(f"{copied}{suffix}"))
                tracks = read_serato(copied)
                self.assertEqual([track.title for track in tracks], ["A"])
            finally:
                writer.close()

    def test_never_writes_sidecars_next_to_the_vendor_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live.sqlite"
            writer = self._wal_database(live)
            try:
                copy_dir = root / "vendor"
                copy_dir.mkdir()
                vendor = copy_dir / "master.sqlite"
                shutil.copy2(live, vendor)
                con, cleanup = open_serato_library(vendor)
                try:
                    con.execute("SELECT 1").fetchone()
                finally:
                    cleanup()
                self.assertEqual(sorted(p.name for p in copy_dir.iterdir()), ["master.sqlite"])
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
