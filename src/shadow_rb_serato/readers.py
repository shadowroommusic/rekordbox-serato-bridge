from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .colors import rgb_from_color_table_index
from .model import CuePoint, Track, pad_rank
from . import serato_markers


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _source_kind(path: str) -> str:
    if path.startswith("apple-music:") or path.startswith("streaming://"):
        return "streaming"
    if path.startswith("/Volumes/") or path.startswith("\\\\"):
        return "removable-or-external"
    return "local"


def beat_anchor_from_analysis(db_dir: str | Path, analysis_path: str) -> int | None:
    """从 rekordbox 本地分析文件（ANLZ）里取第一拍位置（毫秒）。

    rekordbox 把本地曲目的分析结果放在 ``<db_dir>/share/PIONEER/USBANLZ/.../ANLZ0000.DAT``，
    路径记在 ``djmdContent.AnalysisDataPath``。这里只用它给 Serato 的 BeatGrid 当锚点。
    """
    if not analysis_path:
        return None
    try:
        from pyrekordbox import AnlzFile
    except ImportError:  # pragma: no cover - optional dependency
        return None
    full = Path(db_dir) / "share" / analysis_path.lstrip("/")
    if not full.is_file():
        return None
    try:
        anlz = AnlzFile.parse_file(str(full))
    except Exception:  # pragma: no cover - 分析文件损坏时不影响读取
        return None
    for tag in anlz.tags:
        if getattr(tag, "name", "") != "beat_grid":
            continue
        entries = list(getattr(tag.content, "entries", []) or [])
        if entries:
            time_ms = getattr(entries[0], "time", None)
            if time_ms is not None:
                return int(time_ms)
        break
    return None


def read_rekordbox(database: str | Path, db_dir: str | Path) -> list[Track]:
    """Read a Rekordbox 6/7 database through pyrekordbox without writing it."""
    try:
        from pyrekordbox import Rekordbox6Database
    except ImportError as exc:
        raise RuntimeError("Install pyrekordbox before reading a Rekordbox database") from exc
    db = Rekordbox6Database(path=database, db_dir=db_dir, unlock=True)
    try:
        contents = db.get_content().all()
        artists = {_text(item.ID): _text(item.Name) for item in db.get_artist().all()}
        keys = {_text(item.ID): _text(item.ScaleName) for item in db.get_key().all()}
        cues_by_content: dict[str, list[CuePoint]] = {}
        for cue in db.get_cue().all():
            cues_by_content.setdefault(_text(cue.ContentUUID), []).append(
                CuePoint(
                    kind=int(cue.Kind) if cue.Kind is not None else 0,
                    in_ms=int(cue.InMsec) if cue.InMsec is not None else None,
                    out_ms=(int(cue.OutMsec) if cue.OutMsec is not None and int(cue.OutMsec) >= 0 else None),
                    comment=_text(cue.Comment) or None,
                    # rekordbox 的 cue 颜色存的是调色板编号（ColorTableIndex），不是 RGB；
                    # 0 = 不设颜色。
                    color=rgb_from_color_table_index(cue.ColorTableIndex),
                    active_loop=bool(cue.ActiveLoop) if cue.ActiveLoop is not None else None,
                    beat_loop_size=int(cue.BeatLoopSize) if cue.BeatLoopSize is not None else None,
                )
            )
        # pad 字母由 Kind 决定（A..P = 1,2,3,5,…,17），导出去 Serato 时按 pad 顺序排，
        # 这样 rekordbox 的 pad A/B/C 才会落在 Serato 的 cue 1/2/3 上。
        for cues in cues_by_content.values():
            cues.sort(key=lambda cue: (pad_rank(cue.kind), cue.in_ms or 0))
        result: list[Track] = []
        for content in contents:
            path = _text(content.FolderPath)
            result.append(
                Track(
                    source_id=_text(content.UUID),
                    title=_text(content.Title),
                    artist=artists.get(_text(content.ArtistID), _text(content.SrcArtistName)),
                    path=path,
                    file_name=_text(content.FileNameL) or Path(path).name,
                    size=int(content.FileSize) if content.FileSize is not None else None,
                    length_ms=(int(content.Length) * 1000 if content.Length is not None else None),
                    bpm=(float(content.BPM) / 100.0 if content.BPM else None),
                    key=keys.get(_text(content.KeyID)) or None,
                    rating=int(content.Rating) if content.Rating is not None else None,
                    color=_text(content.ColorID) or None,
                    cues=tuple(cues_by_content.get(_text(content.UUID), [])),
                    source_kind=_source_kind(path),
                    beat_anchor_ms=beat_anchor_from_analysis(db_dir, _text(content.AnalysisDataPath)),
                )
            )
        return result
    finally:
        db.close()


def serato_local_path(portable_id: str, file_name: str) -> str:
    """Serato 把本地文件的相对路径存在 portable_id 里（相对卷根目录）。"""
    portable = (portable_id or "").strip()
    if portable and not portable.startswith("streaming://"):
        return portable if portable.startswith("/") else "/" + portable
    return file_name


def read_serato(
    database: str | Path,
    read_markers: bool = True,
    warnings: "list[str] | None" = None,
) -> list[Track]:
    """Read Serato's master.sqlite using SQLite's read-only URI mode.

    Serato DJ Pro 4.x keeps cue points in the audio files, not in master.sqlite,
    so local assets also get their Serato Markers2 / Markers_ tags read here.
    """
    path = Path(database)
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, file_name, portable_id, file_size, name, artist, bpm, key, rating, color, length_ms "
            "FROM asset ORDER BY id"
        ).fetchall()
        tracks: "list[Track]" = []
        for row in rows:
            file_name = _text(row["file_name"])
            streaming = file_name.startswith("streaming://")
            local_path = file_name if streaming else serato_local_path(_text(row["portable_id"]), file_name)
            cues: "tuple[CuePoint, ...]" = ()
            if read_markers and not streaming:
                if Path(local_path).is_file():
                    try:
                        markers, _source = serato_markers.read_markers(local_path)
                        cues = serato_markers.to_cue_points(markers)
                    except Exception as exc:  # pragma: no cover - defensive
                        if warnings is not None:
                            warnings.append(f"could not read Serato markers from {local_path}: {exc}")
                elif warnings is not None:
                    warnings.append(f"local asset not found on disk, markers not read: {local_path}")
            tracks.append(
                Track(
                    source_id=f"serato:{row['id']}",
                    title=_text(row["name"]),
                    artist=_text(row["artist"]),
                    path=local_path,
                    file_name=Path(local_path.replace("\\", "/")).name,
                    size=int(row["file_size"]) if row["file_size"] is not None else None,
                    length_ms=int(row["length_ms"]) if row["length_ms"] is not None else None,
                    bpm=float(row["bpm"]) if row["bpm"] else None,
                    key=_text(row["key"]) or None,
                    rating=row["rating"],
                    color=row["color"],
                    cues=cues,
                    source_kind="streaming" if streaming else "local",
                )
            )
        return tracks
    finally:
        con.close()
