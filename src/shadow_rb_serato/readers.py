from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .model import CuePoint, Track


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _source_kind(path: str) -> str:
    if path.startswith("apple-music:") or path.startswith("streaming://"):
        return "streaming"
    if path.startswith("/Volumes/") or path.startswith("\\\\"):
        return "removable-or-external"
    return "local"


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
                    color=int(cue.Color) if cue.Color is not None else None,
                    active_loop=bool(cue.ActiveLoop) if cue.ActiveLoop is not None else None,
                    beat_loop_size=int(cue.BeatLoopSize) if cue.BeatLoopSize is not None else None,
                )
            )
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
                )
            )
        return result
    finally:
        db.close()


def read_serato(database: str | Path) -> list[Track]:
    """Read Serato's master.sqlite using SQLite's read-only URI mode."""
    path = Path(database)
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, file_name, file_size, name, artist, bpm, key, rating, color, length_ms FROM asset ORDER BY id"
        ).fetchall()
        return [
            Track(
                source_id=f"serato:{row['id']}",
                title=_text(row["name"]),
                artist=_text(row["artist"]),
                path=_text(row["file_name"]),
                file_name=Path(_text(row["file_name"]).replace("\\", "/")).name,
                size=int(row["file_size"]) if row["file_size"] is not None else None,
                length_ms=int(row["length_ms"]) if row["length_ms"] is not None else None,
                bpm=float(row["bpm"]) if row["bpm"] else None,
                key=_text(row["key"]) or None,
                rating=row["rating"],
                color=row["color"],
                source_kind="streaming" if _text(row["file_name"]).startswith("streaming://") else "local",
            )
            for row in rows
        ]
    finally:
        con.close()
