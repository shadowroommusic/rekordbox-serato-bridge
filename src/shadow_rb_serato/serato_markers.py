"""Serato cue / loop 读写（零依赖）。

Serato DJ Pro 4.x 的 master.sqlite 不保存 cue，它把 cue / loop 写在音频文件里：

* MP3 / AIFF：ID3v2 GEOB 帧 ``Serato Markers2``（v2）或 ``Serato Markers_``（v1）
* 其它容器 Serato 走各自的元数据方式，本模块不猜测，遇到就明确报告

读取是只读的；写入函数只用于 staging 副本（见 ``staging.py``）。原始音频文件永远
不会被这个插件修改。
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import io
import struct
from pathlib import Path

from .model import CuePoint

MARKERS2_DESC = "Serato Markers2"
MARKERS1_DESC = "Serato Markers_"
BEATGRID_DESC = "Serato BeatGrid"
MARKERS2_VERSION = b"\x01\x01"
MARKERS1_VERSION = b"\x02\x05"


@dataclass(frozen=True)
class SeratoMarker:
    kind: str
    index: int | None
    position_ms: int | None
    end_ms: int | None = None
    name: str | None = None
    color: int | None = None
    locked: bool | None = None
    source: str = MARKERS2_DESC


def _read_cstring(handle: io.BytesIO) -> bytes:
    chunks = []
    while True:
        byte = handle.read(1)
        if byte in (b"", b"\x00"):
            return b"".join(chunks)
        chunks.append(byte)


def _color_to_int(raw: bytes) -> int | None:
    if len(raw) < 3:
        return None
    red, green, blue = raw[0], raw[1], raw[2]
    return (red << 16) | (green << 8) | blue


def decode_bytes32(raw: bytes) -> bytes:
    """Serato v1 的 4 字节编码 → 3 字节明文。"""
    w, x, y, z = struct.unpack("BBBB", raw)
    c = (z & 0x7F) | ((y & 0x01) << 7)
    b = ((y & 0x7F) >> 1) | ((x & 0x03) << 6)
    a = ((x & 0x7F) >> 2) | ((w & 0x07) << 5)
    return struct.pack("BBB", a, b, c)


def encode_bytes32(raw: bytes) -> bytes:
    """3 字节明文 → Serato v1 的 4 字节编码。"""
    a, b, c = struct.unpack("BBB", raw)
    z = c & 0x7F
    y = ((c >> 7) | (b << 1)) & 0x7F
    x = ((b >> 6) | (a << 2)) & 0x7F
    w = a >> 5
    return bytes((w, x, y, z))


def _decode_position(raw: bytes) -> int | None:
    if raw == b"\x7f\x7f\x7f\x7f":
        return None
    return struct.unpack(">I", decode_bytes32(raw).rjust(4, b"\x00"))[0]


def _encode_position(position: int | None) -> bytes:
    if position is None:
        return b"\x7f\x7f\x7f\x7f"
    return encode_bytes32(struct.pack(">I", position)[1:])


def parse_markers2(data: bytes, source: str = MARKERS2_DESC) -> "list[SeratoMarker]":
    """解析 ``Serato Markers2`` GEOB 数据。"""
    if data[:2] != MARKERS2_VERSION:
        raise ValueError(f"unsupported Serato Markers2 version: {data[:2]!r}")
    try:
        end = data.index(b"\x00", 2)
    except ValueError:
        end = len(data)
    encoded = data[2:end].replace(b"\n", b"").replace(b"\r", b"")
    padding = b"A==" if len(encoded) % 4 == 1 else b"=" * (-len(encoded) % 4)
    payload = base64.b64decode(encoded + padding)
    if payload[:2] != MARKERS2_VERSION:
        raise ValueError(f"unsupported Markers2 payload version: {payload[:2]!r}")
    handle = io.BytesIO(payload[2:])
    markers: "list[SeratoMarker]" = []
    while True:
        name = _read_cstring(handle)
        if not name:
            break
        raw_length = handle.read(4)
        if len(raw_length) < 4:
            break
        body = handle.read(struct.unpack(">I", raw_length)[0])
        if name == b"CUE":
            field1, index, position, field4, color, field6 = struct.unpack(">cBIc3s2s", body[:12])
            label = body[12:].split(b"\x00")[0].decode("utf-8", "replace")
            markers.append(
                SeratoMarker("cue", index, position, None, label or None, _color_to_int(color), None, source)
            )
        elif name == b"LOOP":
            # Serato 官方 LOOP 布局（对齐 Mixxx 的 SeratoMarkers2LoopEntry）：
            #   00 | index | start(4) | end(4) | FF FF FF FF | 00 | R | G | B | 00 | locked | label
            if len(body) >= 20 and body[10:14] == b"\xff\xff\xff\xff":
                _, index, start, finish, _, _, red, green, blue, _, locked = struct.unpack(
                    ">cBII4sBBBBB?", body[:20]
                )
                color = (red << 16) | (green << 8) | blue
            else:
                # 兼容本工具 0.2.x 写出的旧布局（magic 全 0、颜色只有 1 字节）
                _, index, start, finish, _, _, color, locked = struct.unpack(">cBII4s4sB?", body[:20])
            label = body[20:].split(b"\x00")[0].decode("utf-8", "replace")
            markers.append(
                SeratoMarker("loop", index, start, finish, label or None, color, locked, source)
            )
    return markers


def parse_markers1(data: bytes, source: str = MARKERS1_DESC) -> "list[SeratoMarker]":
    """解析 ``Serato Markers_`` GEOB 数据（固定 22 字节记录）。"""
    if data[:2] != MARKERS1_VERSION:
        raise ValueError(f"unsupported Serato Markers_ version: {data[:2]!r}")
    handle = io.BytesIO(data[2:])
    raw_count = handle.read(4)
    if len(raw_count) < 4:
        raise ValueError("truncated Serato Markers_ header")
    markers: "list[SeratoMarker]" = []
    for _ in range(struct.unpack(">I", raw_count)[0]):
        entry = handle.read(0x16)
        if len(entry) != 0x16:
            raise ValueError("truncated Serato Markers_ entry")
        (
            start_set,
            start_raw,
            end_set,
            end_raw,
            _field5,
            color_raw,
            entry_type,
            locked,
        ) = struct.unpack(">B4sB4s6s4sBB", entry)
        start = _decode_position(start_raw) if start_set != 0x7F else None
        end = _decode_position(end_raw) if end_set != 0x7F else None
        kind = "loop" if entry_type == 3 else "cue"
        markers.append(
            SeratoMarker(
                kind,
                None,
                start,
                end if kind == "loop" else None,
                None,
                _color_to_int(decode_bytes32(color_raw)),
                bool(locked),
                source,
            )
        )
    return markers


def parse_geob_tags(payload: bytes) -> "dict[str, bytes]":
    """从一段 ID3v2 标签字节里取出所有 GEOB 帧：desc → data。"""
    if len(payload) < 10 or payload[:3] != b"ID3":
        return {}
    major = payload[3]
    flags = payload[5]
    size_bytes = payload[6:10]
    size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    body = payload[10 : 10 + size]
    if flags & 0x40:  # extended header: skip it
        if major == 4:
            ext_size = (body[0] << 21) | (body[1] << 14) | (body[2] << 7) | body[3]
        else:
            ext_size = struct.unpack(">I", body[0:4])[0] + 4
        body = body[ext_size:]
    tags: "dict[str, bytes]" = {}
    position = 0
    while position + 10 <= len(body):
        frame_id = body[position : position + 4]
        if frame_id.strip(b"\x00") == b"":
            break
        raw = body[position + 4 : position + 8]
        if major == 4:
            frame_size = (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]
        else:
            frame_size = struct.unpack(">I", raw)[0]
        frame_body = body[position + 10 : position + 10 + frame_size]
        if frame_id.startswith(b"GEOB"):
            parts = frame_body.split(b"\x00")
            if len(parts) >= 4:
                desc = parts[3].decode("latin-1")
                data = b"\x00".join(parts[4:])
                tags[desc] = data
        position += 10 + frame_size
    return tags


def parse_beatgrid_bpm(data: bytes) -> float | None:
    """从 ``Serato BeatGrid`` GEOB 里取 terminal marker 的 BPM。

    布局（对齐 Mixxx 的 SeratoBeatGrid::parseID3）：version(2) + numMarkers(4) +
    非 terminal marker（每条 8 字节：position f32 + beatsTillNext u32）+ terminal
    marker（position f32 + bpm f32）+ footer(1)。
    """
    if len(data) < 14:
        return None
    count = struct.unpack(">I", data[2:6])[0]
    if count < 1:
        return None
    terminal = 6 + (count - 1) * 8
    if terminal + 8 > len(data):
        return None
    _position, bpm = struct.unpack(">ff", data[terminal : terminal + 8])
    return float(bpm) if bpm and bpm > 0 else None


def read_beatgrid_bpm(path: str | Path) -> float | None:
    """文件的 ``Serato BeatGrid`` 标签里的 BPM（Serato 库里 BPM 列经常是空的）。"""
    try:
        tag = extract_id3_tag(path)
    except OSError:
        return None
    if not tag:
        return None
    payload = parse_geob_tags(tag).get(BEATGRID_DESC)
    if not payload:
        return None
    try:
        return parse_beatgrid_bpm(payload)
    except (struct.error, ValueError):
        return None


def extract_id3_tag(path: str | Path) -> bytes | None:
    """读取 MP3 开头、AIFF ``ID3 `` chunk 或 WAV ``id3 `` chunk 里的 ID3v2 标签。"""
    target = Path(path)
    with open(target, "rb") as handle:
        head = handle.read(12)
        if head[:3] == b"ID3":
            handle.seek(0)
            return _read_id3(handle)
        if head[:4] == b"FORM":
            handle.seek(12)
            while True:
                chunk_header = handle.read(8)
                if len(chunk_header) < 8:
                    return None
                chunk_id = chunk_header[:4]
                chunk_size = struct.unpack(">I", chunk_header[4:])[0]
                if chunk_id == b"ID3 ":
                    return handle.read(chunk_size)
                handle.seek(chunk_size + (chunk_size & 1), io.SEEK_CUR)
        if head[:4] == b"RIFF":
            handle.seek(12)
            while True:
                chunk_header = handle.read(8)
                if len(chunk_header) < 8:
                    return None
                chunk_id = chunk_header[:4]
                chunk_size = struct.unpack("<I", chunk_header[4:])[0]
                if chunk_id in (b"id3 ", b"ID3 "):
                    return handle.read(chunk_size)
                handle.seek(chunk_size + (chunk_size & 1), io.SEEK_CUR)
    return None


def container_of(path: str | Path) -> str:
    """按文件头判断容器：mp3 / aiff / wav / unknown。"""
    with open(Path(path), "rb") as handle:
        head = handle.read(12)
    if head[:3] == b"ID3":
        return "mp3"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:2] in (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    return "unknown"


def _read_id3(handle) -> bytes:
    header = handle.read(10)
    size_bytes = header[6:10]
    size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    return header + handle.read(size)


def read_markers(path: str | Path) -> "tuple[list[SeratoMarker], str]":
    """读取一个音频文件的 Serato cue/loop，返回 (markers, 来源描述)。"""
    try:
        tag = extract_id3_tag(path)
    except OSError as exc:
        return [], f"unreadable: {exc}"
    if not tag:
        return [], "no-id3-tag"
    tags = parse_geob_tags(tag)
    if MARKERS2_DESC in tags:
        return parse_markers2(tags[MARKERS2_DESC]), MARKERS2_DESC
    if MARKERS1_DESC in tags:
        return parse_markers1(tags[MARKERS1_DESC]), MARKERS1_DESC
    return [], "no-serato-markers"


def to_cue_points(markers: "list[SeratoMarker]") -> "tuple[CuePoint, ...]":
    """把 Serato 标记映射成 bridge 的统一 Cue 模型（kind 1 = hot cue）。"""
    points: "list[CuePoint]" = []
    for marker in markers:
        if marker.kind == "loop":
            size = None
            if marker.position_ms is not None and marker.end_ms is not None:
                size = marker.end_ms - marker.position_ms
            points.append(
                CuePoint(1, marker.position_ms, marker.end_ms, marker.name, marker.color, True, size)
            )
        else:
            points.append(CuePoint(1, marker.position_ms, None, marker.name, marker.color, False, None))
    return tuple(points)


def from_cue_points(cues) -> "list[SeratoMarker]":
    """把统一 Cue 模型（或 cue 字典）转成 Serato 标记，供 staging 写入使用。"""
    markers: "list[SeratoMarker]" = []
    for index, cue in enumerate(cues):
        if isinstance(cue, dict):
            name = cue.get("name") or cue.get("comment")
            position = cue.get("position_ms", cue.get("in_ms"))
            end = cue.get("end_ms", cue.get("out_ms"))
            color = cue.get("color")
            kind = cue.get("kind")
        else:
            name = cue.comment
            position = cue.in_ms
            end = cue.out_ms
            color = cue.color
            kind = "loop" if cue.active_loop or cue.out_ms is not None else "cue"
        is_loop = kind in ("loop", 2, 3) or end is not None
        if isinstance(color, int):
            color = int(color) & 0xFFFFFF
        elif isinstance(color, str) and color.strip().lstrip("#"):
            text = color.strip().lstrip("#")
            color = int(text, 16) if len(text) == 6 else None
        else:
            color = None
        markers.append(
            SeratoMarker(
                "loop" if is_loop else "cue",
                index,
                int(position) if position is not None else None,
                int(end) if is_loop and end is not None else None,
                str(name) if name else None,
                color,
                False if is_loop else None,
                MARKERS2_DESC,
            )
        )
    return markers


def build_markers2(markers: "list[SeratoMarker]") -> bytes:
    """构造 ``Serato Markers2`` GEOB 数据（用于 staging 副本）。"""
    payload = bytearray(MARKERS2_VERSION)
    for marker in markers:
        if marker.kind == "cue":
            body = struct.pack(
                ">cBIc3s2s",
                b"\x00",
                marker.index if marker.index is not None else 0,
                marker.position_ms or 0,
                b"\x00",
                _int_to_color(marker.color),
                b"\x00\x00",
            )
            payload += b"CUE\x00" + struct.pack(">I", len(body) + len(marker.name or "") + 1)
            payload += body + (marker.name or "").encode("utf-8") + b"\x00"
        elif marker.kind == "loop":
            # Serato 固定给 loop 用的颜色（Mixxx 里叫 kFixedLoopColor）：Serato 本身不读它，
            # 但写上能让文件结构和 Serato 自己写的一模一样。
            body = struct.pack(
                ">cBII4sBBBBB?",
                b"\x00",
                marker.index if marker.index is not None else 0,
                marker.position_ms or 0,
                marker.end_ms or 0,
                b"\xff\xff\xff\xff",
                0,
                0x27,
                0xAA,
                0xE1,
                0,
                bool(marker.locked),
            )
            payload += b"LOOP\x00" + struct.pack(">I", len(body) + len(marker.name or "") + 1)
            payload += body + (marker.name or "").encode("utf-8") + b"\x00"
    payload += b"\x00"
    encoded = bytearray(base64.b64encode(bytes(payload)).replace(b"=", b"A"))
    index = 72
    while index < len(encoded):
        encoded.insert(index, 0x0A)
        index += 73
    # Serato 会把整个 GEOB 数据补齐到 470 字节，解析方靠结尾的 \x00 判断 base64 结束。
    return (MARKERS2_VERSION + bytes(encoded)).ljust(470, b"\x00")


def _int_to_color(value: int | None) -> bytes:
    number = int(value or 0)
    return bytes(((number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF))


def build_geob_frame(desc: str, data: bytes, major: int = 4) -> bytes:
    """GEOB 帧：encoding + mime + filename + description + data（按标签版本编码长度）。"""
    body = b"\x00" + b"application/octet-stream\x00" + b"\x00" + desc.encode("latin-1") + b"\x00" + data
    size = _synchsafe(len(body)) if major >= 4 else struct.pack(">I", len(body))
    return b"GEOB" + size + b"\x00\x00" + body


def _synchsafe(value: int) -> bytes:
    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def replace_geob_tags(path: str | Path, frames: "dict[str, bytes]", *, tag_padding: int = 1024) -> None:
    """把 GEOB 帧写进文件的 ID3 标签（只应作用于 staging 副本）。

    现有标签里的其它帧会被保留；标签整体重建为 ID3v2.4。目前只对 MP3 和 AIFF
    实现写入，其它容器会明确报错，避免把文件写坏。
    """
    target = Path(path)
    container = container_of(target)
    if container == "mp3":
        _write_mp3_tag(target, frames, tag_padding)
        return
    if container == "aiff":
        _write_aiff_tag(target, frames, tag_padding)
        return
    raise ValueError(f"staging writer supports MP3 and AIFF for now, got '{container}': {target.name}")


def _build_tag(target: Path, frames: "dict[str, bytes]", tag_padding: int) -> bytes:
    existing = extract_id3_tag(target)
    major = existing[3] if existing else 4
    kept = bytearray()
    if existing:
        for desc, frame in _iter_raw_frames(existing):
            if desc and desc in frames:
                continue  # 替换同名 GEOB，其余帧按原样保留
            kept += frame
    body = bytes(kept) + b"".join(build_geob_frame(desc, payload, major) for desc, payload in frames.items())
    body += b"\x00" * tag_padding
    return b"ID3" + bytes([major, 0, 0]) + _synchsafe(len(body)) + body


def _write_mp3_tag(target: Path, frames: "dict[str, bytes]", tag_padding: int) -> None:
    tag = _build_tag(target, frames, tag_padding)
    with open(target, "rb") as handle:
        data = handle.read()
    if data[:3] == b"ID3":
        size_bytes = data[6:10]
        size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
        data = data[10 + size :]
    with open(target, "wb") as handle:
        handle.write(tag + data)


def _write_aiff_tag(target: Path, frames: "dict[str, bytes]", tag_padding: int) -> None:
    tag = _build_tag(target, frames, tag_padding)
    with open(target, "rb") as handle:
        data = handle.read()
    if data[:4] != b"FORM":
        raise ValueError(f"not an AIFF file: {target.name}")
    form_type = data[8:12]
    chunks: "list[bytes]" = []
    position = 12
    while position + 8 <= len(data):
        chunk_id = data[position : position + 4]
        chunk_size = struct.unpack(">I", data[position + 4 : position + 8])[0]
        body = data[position + 8 : position + 8 + chunk_size]
        padding = b"\x00" if chunk_size & 1 else b""
        if chunk_id != b"ID3 ":
            chunks.append(chunk_id + struct.pack(">I", len(body)) + body + padding)
        position += 8 + chunk_size + (chunk_size & 1)
    id3_chunk = b"ID3 " + struct.pack(">I", len(tag)) + tag + (b"\x00" if len(tag) & 1 else b"")
    payload = form_type + b"".join(chunks) + id3_chunk
    with open(target, "wb") as handle:
        handle.write(b"FORM" + struct.pack(">I", len(payload)) + payload)


def _iter_raw_frames(payload: bytes):
    """遍历 ID3v2 标签里的原始帧，产出 (描述, 帧字节)。"""
    if len(payload) < 10 or payload[:3] != b"ID3":
        return
    major = payload[3]
    size_bytes = payload[6:10]
    size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    body = payload[10 : 10 + size]
    position = 0
    while position + 10 <= len(body):
        frame_id = body[position : position + 4]
        if frame_id.strip(b"\x00") == b"":
            return
        raw_size = body[position + 4 : position + 8]
        if major == 4:
            frame_size = (raw_size[0] << 21) | (raw_size[1] << 14) | (raw_size[2] << 7) | raw_size[3]
        else:
            frame_size = struct.unpack(">I", raw_size)[0]
        frame = body[position : position + 10 + frame_size]
        desc = ""
        if frame_id.startswith(b"GEOB"):
            parts = frame[10:].split(b"\x00")
            if len(parts) >= 4:
                desc = parts[3].decode("latin-1")
        yield desc, frame
        position += 10 + frame_size
