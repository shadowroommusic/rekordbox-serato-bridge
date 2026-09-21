from __future__ import annotations

import sqlite3
import shutil
import tempfile
import os
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


def open_serato_library(path: "str | Path") -> "tuple[sqlite3.Connection, Any]":
    """打开 Serato 的 master.sqlite，且不改动 Serato 自己的目录。

    Serato DJ Pro 把库放在 WAL 模式里。这种库不能简单用 ``?mode=ro`` 打开：SQLite 需要创建
    ``-shm`` 边车文件，只读连接做不到，于是抛 “unable to open database file”（真机验证：Serato
    4.x 的 master.sqlite 在没有 -wal/-shm 时会稳定复现）。``immutable=1`` 能打开，但会**静默读到
    上一次 checkpoint 的旧数据**，所以不能拿它兜底。

    因此：先试严格只读；失败就把库连同 ``-wal``/``-shm`` 一起复制到临时目录，打开副本读取，用完
    删掉。vendor 目录始终只有读操作。

    Returns:
        (connection, cleanup)：读完必须调用 cleanup()。
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1").fetchone()  # connect() 是懒的：这里才真正打开
        return con, con.close
    except sqlite3.OperationalError:
        pass

    workdir = Path(tempfile.mkdtemp(prefix="shadow-serato-"))
    target = workdir / source.name
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{source}{suffix}")
        if sidecar.is_file():
            shutil.copy2(sidecar, Path(f"{target}{suffix}"))
    con = sqlite3.connect(str(target))
    con.row_factory = sqlite3.Row

    def cleanup() -> None:
        con.close()
        shutil.rmtree(workdir, ignore_errors=True)

    return con, cleanup


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
    """Serato 把本地文件的相对路径存在 portable_id 里（相对卷根目录）。

    平台门槛：POSIX 卷相对路径要补前导 `/`；Windows 上 portable 可能是
    `C:/Music/x.wav`（盘符绝对）或 `Music\\x.wav`（卷相对，分隔符是反斜杠）——
    只有「既无前导 / 又无盘符」才补根，且补的是平台正确的根。
    """
    portable = (portable_id or "").strip().replace("\\", "/")
    if not portable or portable.startswith("streaming://"):
        return file_name
    has_drive = len(portable) >= 2 and portable[1] == ":" and portable[0].isalpha()
    if portable.startswith("/") or has_drive:
        return portable
    return "/" + portable if os.name != "nt" else portable


def read_serato(
    database: str | Path,
    read_markers: bool = True,
    warnings: "list[str] | None" = None,
) -> list[Track]:
    """Read Serato's master.sqlite using SQLite's read-only URI mode.

    Serato DJ Pro 4.x keeps cue points in the audio files, not in master.sqlite,
    so local assets also get their Serato Markers2 / Markers_ tags read here.
    """
    con, cleanup = open_serato_library(database)
    try:
        try:
            rows = con.execute(
                "SELECT id, file_name, portable_id, file_size, name, artist, bpm, key, rating, color, length_ms "
                "FROM asset ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise RuntimeError(
                    "Serato 的 master.sqlite 里读不到 asset 表：这份库看起来只复制了主文件，"
                    "-wal/-shm 边车里还没合并的数据不在。请退出 Serato 后重新复制，或直接把 "
                    "serato_database 指向 Serato 自己的库目录。"
                ) from exc
            raise
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
        cleanup()
