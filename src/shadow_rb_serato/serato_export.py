"""把一个 Rekordbox set 转成 Serato 可直接使用的输出。

典型场景：在 rekordbox 里排好 set → 用这个模块转换到 U 盘 → 现场只有 Serato 的机器
插上就能看到同样的 cue / loop / 播放列表。

输出结构（可直接整体拷到 U 盘根目录）::

    <out>/ShadowRoom/<set 名>/<曲目文件>      # 带 Serato Markers2 / BeatGrid 标签的副本
    <out>/_Serato_/Subcrates/<set 名>.crate   # Serato 播放列表（crate）
    <out>/ShadowRoom/<set 名>/manifest.json   # 写入与校验记录

原始文件、Rekordbox 数据库、Serato 数据库都不会被修改。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import struct

from . import serato_markers
from .colors import SERATO_DEFAULT_RGB
from .model import CuePoint
from .serato_markers import MARKERS2_DESC, SeratoMarker

BEATGRID_DESC = "Serato BeatGrid"
BEATGRID_VERSION = b"\x01\x00"
EXPORT_ROOT_NAME = "ShadowRoom"


@dataclass(frozen=True)
class SetTrack:
    id: str
    title: str
    artist: str
    path: str
    bpm: float | None = None
    key: str | None = None
    duration_ms: int | None = None
    cues: "tuple[CuePoint, ...]" = field(default_factory=tuple)


@dataclass(frozen=True)
class RekordboxSet:
    name: str
    tracks: "list[SetTrack]"


def cue_to_marker(index: int, cue: CuePoint) -> SeratoMarker:
    """Rekordbox 的 memory cue / hot cue / loop → Serato 标记。"""
    is_loop = bool(cue.active_loop) or (cue.out_ms is not None)
    # rekordbox 侧已经按调色板编号换算成 RGB（readers.read_rekordbox）；
    # 没设颜色的 cue 用 Serato 的默认白色，避免写成黑色。
    color = cue.color if isinstance(cue.color, int) and cue.color >= 0 else SERATO_DEFAULT_RGB
    if is_loop and cue.out_ms is not None:
        return SeratoMarker("loop", index, cue.in_ms, cue.out_ms, cue.comment, color, False, MARKERS2_DESC)
    return SeratoMarker("cue", index, cue.in_ms, None, cue.comment, color, None, MARKERS2_DESC)


def markers_for(track: SetTrack) -> "list[SeratoMarker]":
    """Rekordbox cue/loop → Serato 标记，cue 和 saved loop 各用一套槽位号。

    Serato 里 hot cue 和 saved loop 是两套独立的槽位（各 0..7）：CUE 条目的 index=0
    就是 hot cue 1，LOOP 条目的 index=0 就是 saved loop 1。如果让 loop 也占 cue 的序号，
    它后面的 cue 会在 Serato 里串位（实机确认过 index=0/1 → cue 1/2）。
    """
    markers: "list[SeratoMarker]" = []
    next_cue = 0
    next_loop = 0
    for cue in track.cues:
        is_loop = bool(cue.active_loop) or cue.out_ms is not None
        if is_loop:
            markers.append(cue_to_marker(next_loop, cue))
            next_loop += 1
        else:
            markers.append(cue_to_marker(next_cue, cue))
            next_cue += 1
    return markers


def build_beatgrid(bpm: float | None, anchor_ms: int = 0) -> bytes:
    """Serato BeatGrid：单条 terminal marker（位置 + BPM）就足够恒定速度的曲目。"""
    if not bpm or bpm <= 0:
        raise ValueError("beatgrid needs a positive bpm")
    payload = BEATGRID_VERSION + struct.pack(">I", 1)
    payload += struct.pack(">f", anchor_ms / 1000.0) + struct.pack(">f", float(bpm))
    payload += b"\x00"
    return payload


def _write_crate(crate_path: Path, tracks_paths: "list[str]") -> Path:
    """写 Serato 传统 crate 文件（ScratchLive 格式，Serato 从 _Serato_/Subcrates 读取）。"""
    crate_path.parent.mkdir(parents=True, exist_ok=True)
    crate_dir = crate_path.parent  # .../_Serato_/Subcrates
    track_root = crate_dir.parent.parent  # .../

    def relative(path: str) -> str:
        resolved = Path(path).expanduser().resolve()
        try:
            return str(resolved.relative_to(track_root.resolve())).replace("\\", "/")
        except ValueError:
            return str(resolved).lstrip("/").replace("\\", "/")

    def item(field: str, value) -> bytes:
        if field == "vrsn":
            raw = value.encode("utf-16-be")
        elif isinstance(value, str):
            raw = value.encode("utf-16-be")
        elif isinstance(value, bool):
            raw = struct.pack("?", value)
        elif isinstance(value, int):
            raw = struct.pack(">I", value)
        else:
            raw = bytes(value)
        return field.encode("ascii") + struct.pack(">I", len(raw)) + raw

    data = bytearray()
    data += item("vrsn", "1.0/Serato ScratchLive Crate")
    sorting = item("tvcn", "key") + item("brev", False)
    data += b"osrt" + struct.pack(">I", len(sorting)) + sorting
    for column in ("song", "playCount", "artist", "bpm", "key", "album", "length", "comment", "added"):
        body = item("tvcn", column) + item("tvcw", "0")
        data += b"ovct" + struct.pack(">I", len(body)) + body
    for path in tracks_paths:
        body = item("ptrk", relative(path))
        data += b"otrk" + struct.pack(">I", len(body)) + body
    crate_path.write_bytes(bytes(data))
    return crate_path


def convert_set(
    tracks: "list[SetTrack]",
    out_dir: str | Path,
    *,
    name: str,
    crate_dir: str | Path | None = None,
    write_beatgrid: bool = True,
) -> dict:
    """复制曲目、写 Serato 标签、生成 crate 与 manifest。"""
    out_root = Path(out_dir).expanduser()
    album_dir = out_root / EXPORT_ROOT_NAME / name
    album_dir.mkdir(parents=True, exist_ok=True)
    crate_path = (Path(crate_dir).expanduser() if crate_dir else out_root / "_Serato_" / "Subcrates") / f"{name}.crate"

    entries: "list[dict]" = []
    staged_paths: "list[str]" = []
    for index, track in enumerate(tracks):
        source = Path(track.path).expanduser()
        entry: "dict" = {
            "id": track.id,
            "title": track.title,
            "artist": track.artist,
            "source": str(source),
            "cues": [{"kind": cue.kind, "in_ms": cue.in_ms, "out_ms": cue.out_ms, "comment": cue.comment} for cue in track.cues],
        }
        if not source.is_file():
            entry.update({"status": "missing", "error": f"file not found: {source}"})
            entries.append(entry)
            continue
        target = album_dir / source.name
        if target.exists():
            target = album_dir / f"{source.stem}-{index + 1}{source.suffix}"
        original = source.read_bytes()
        shutil.copy2(source, target)
        markers = markers_for(track)
        frames: "dict[str, bytes]" = {}
        if markers:
            frames[MARKERS2_DESC] = serato_markers.build_markers2(markers)
        if write_beatgrid and track.bpm:
            anchor = track.cues[0].in_ms if track.cues and track.cues[0].in_ms else 0
            frames[BEATGRID_DESC] = build_beatgrid(track.bpm, anchor or 0)
        try:
            if frames:
                serato_markers.replace_geob_tags(target, frames)
            read_back, marker_source = serato_markers.read_markers(target)
        except Exception as exc:
            entry.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}", "staged": str(target)})
            entries.append(entry)
            continue
        expected = [(marker.kind, marker.position_ms, marker.end_ms, marker.name) for marker in markers]
        actual = [(marker.kind, marker.position_ms, marker.end_ms, marker.name) for marker in read_back]
        entry.update(
            {
                "status": "converted" if expected == actual else "verification-failed",
                "staged": str(target),
                "markers": len(read_back),
                "marker_source": marker_source,
                "verification": {"expected": expected, "actual": actual, "matches": expected == actual},
                "source_unchanged": source.read_bytes() == original,
            }
        )
        if expected == actual:
            staged_paths.append(str(target))
        entries.append(entry)

    crate = _write_crate(crate_path, staged_paths) if staged_paths else None
    report = {
        "schema_version": 1,
        "mode": "set-conversion",
        "set": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "track_count": len(tracks),
        "converted": sum(entry["status"] == "converted" for entry in entries),
        "missing": sum(entry["status"] == "missing" for entry in entries),
        "failed": sum(entry["status"] in {"failed", "verification-failed"} for entry in entries),
        "out_dir": str(out_root),
        "album_dir": str(album_dir),
        "crate": str(crate) if crate else None,
        "tracks": entries,
        "warnings": [
            "Only copies were written; the original audio files and both vendor databases were left untouched.",
            "Copy the whole output folder to the root of your USB drive so the crate lands in _Serato_/Subcrates.",
        ],
    }
    manifest_path = album_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["manifest"] = str(manifest_path)
    return report


def read_device_tracks(device_root: str | Path, *, paths: "list[str] | None" = None) -> "list[SetTrack]":
    """从 rekordbox U 盘的 USBANLZ 读取曲目与 cue（设备库本身是加密的，ANLZ 可读）。"""
    try:
        from pyrekordbox.anlz import AnlzFile
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("reading a rekordbox device needs pyrekordbox installed") from exc

    root = Path(device_root).expanduser()
    anlz_root = root / "PIONEER" / "USBANLZ"
    if not anlz_root.is_dir():
        raise FileNotFoundError(f"not a rekordbox device: {root}")
    wanted = {str(Path(item)) for item in paths} if paths else None

    tracks: "list[SetTrack]" = []
    for dat in sorted(anlz_root.glob("*/*/ANLZ0000.DAT")):
        try:
            parsed = AnlzFile.parse_file(dat)
            device_path = str(parsed.get("PPTH"))
        except Exception:
            continue
        file_path = root / device_path.lstrip("/")
        if wanted is not None and str(file_path) not in wanted:
            continue
        cues: "list[CuePoint]" = []
        try:
            tag = parsed.get("PCOB")
        except Exception:
            tag = None
        for entry in getattr(tag, "entries", []) or []:
            cue_type = str(getattr(entry, "type", "") or "")
            start = getattr(entry, "time", None)
            end = getattr(entry, "loop_time", None)
            if end in (0xFFFFFFFF, None):
                end = None
            slot = getattr(entry, "hot_cue", None)
            label = chr(64 + int(slot)) if isinstance(slot, int) and 1 <= int(slot) <= 26 else None
            is_loop = "loop" in cue_type.lower()
            cues.append(
                CuePoint(
                    1,
                    int(start) if start is not None else None,
                    int(end) if is_loop and end is not None else None,
                    label,
                    None,
                    is_loop,
                    (int(end) - int(start)) if is_loop and end is not None and start is not None else None,
                )
            )
        tracks.append(
            SetTrack(
                id=device_path,
                title=Path(device_path).stem,
                artist="",
                path=str(file_path),
                cues=tuple(cues),
            )
        )
    return tracks


def to_set_track(track) -> SetTrack:
    """readers.Track → SetTrack。"""
    return SetTrack(
        id=track.source_id,
        title=track.title,
        artist=track.artist,
        path=track.path,
        bpm=track.bpm,
        key=track.key,
        duration_ms=track.length_ms,
        cues=tuple(track.cues),
    )


def read_rekordbox_sets(database: str | Path, db_dir: str | Path) -> "list[RekordboxSet]":
    """读取本地 Rekordbox 库里的播放列表（= set）及其曲目。"""
    try:
        from pyrekordbox import Rekordbox6Database
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("reading Rekordbox needs pyrekordbox installed") from exc
    from .readers import read_rekordbox

    tracks_by_id = {track.source_id: track for track in read_rekordbox(database, db_dir)}
    db = Rekordbox6Database(path=database, db_dir=db_dir, unlock=True)
    try:
        # 播放列表成员用数字 ContentID 引用曲目，而我们的 Track 用 UUID 作为 key。
        id_to_uuid = {str(content.ID): str(content.UUID) for content in db.get_content().all()}
        sets: "list[RekordboxSet]" = []
        for playlist in db.get_playlist().all():
            name = str(getattr(playlist, "Name", "") or "")
            rows = db.get_playlist_songs(PlaylistID=playlist.ID).all()
            members: "list[SetTrack]" = []
            for row in rows:
                content_id = str(getattr(row, "ContentID", "") or "")
                track = tracks_by_id.get(id_to_uuid.get(content_id, content_id))
                if track is not None:
                    members.append(to_set_track(track))
            sets.append(RekordboxSet(name, members))
        return sets
    finally:
        db.close()
