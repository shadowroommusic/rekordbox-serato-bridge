"""Serato → Rekordbox 转换（反向）。

Serato 的 cue/loop 在音频文件里，Rekordbox 的 cue 在数据库里，所以反向转换有两种落点：

1. :func:`write_rekordbox_set` —— 直接写本地 Rekordbox 库（默认 dry-run；写入前自动
   备份 master.db，写完回读校验；Rekordbox 必须处于关闭状态）。
2. :func:`to_rekordbox_xml` —— 生成 rekordbox 兼容的 ``DJ_PLAYLISTS`` XML，
   完全不碰数据库，适合想要人工确认或跨机器搬运的场景。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
from urllib.parse import quote
import xml.etree.ElementTree as ElementTree

from . import serato_markers
from .model import CuePoint
from .readers import serato_local_path
from .serato_export import SetTrack

BACKUP_SUFFIX = "shadow-backup"


@dataclass(frozen=True)
class SeratoSet:
    name: str
    tracks: "list[SetTrack]" = field(default_factory=list)


def _asset_tracks(connection: sqlite3.Connection, asset_ids: "list[int] | None", warnings: "list[str]") -> "list[SetTrack]":
    if asset_ids is None:
        rows = connection.execute(
            "SELECT id, file_name, portable_id, name, artist, bpm, key, length_ms FROM asset ORDER BY id"
        ).fetchall()
    else:
        placeholders = ",".join("?" for _ in asset_ids)
        rows = connection.execute(
            f"SELECT id, file_name, portable_id, name, artist, bpm, key, length_ms FROM asset WHERE id IN ({placeholders})",
            asset_ids,
        ).fetchall()
    tracks: "list[SetTrack]" = []
    for row in rows:
        asset_id, file_name, portable_id, name, artist, bpm, key, length_ms = row
        if str(file_name).startswith("streaming://"):
            continue
        path = serato_local_path(str(portable_id or ""), str(file_name))
        cues: "tuple[CuePoint, ...]" = ()
        if Path(path).is_file():
            try:
                markers, _source = serato_markers.read_markers(path)
                cues = serato_markers.to_cue_points(markers)
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(f"could not read Serato markers from {path}: {exc}")
        else:
            warnings.append(f"local file missing, markers not read: {path}")
        tracks.append(
            SetTrack(
                id=f"serato:{asset_id}",
                title=str(name or Path(path).stem),
                artist=str(artist or ""),
                path=path,
                bpm=float(bpm) if bpm else None,
                key=str(key) if key else None,
                duration_ms=int(length_ms) if length_ms else None,
                cues=cues,
            )
        )
    return tracks


def read_serato_sets(database: str | Path, *, include_all_local: bool = True) -> "list[SeratoSet]":
    """读取 Serato 的 crates（含曲目与文件里的 cue）；可选附加「全部本地曲目」。"""
    path = Path(database)
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    warnings: "list[str]" = []
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(container_asset)").fetchall()}
        link_column = "location_container_id" if "location_container_id" in columns else "container_id"
        sets: "list[SeratoSet]" = []
        for crate_id, crate_name in connection.execute("SELECT id, name FROM container WHERE type = 1 ORDER BY name").fetchall():
            asset_ids = [
                row[0]
                for row in connection.execute(
                    f"SELECT asset_id FROM container_asset WHERE {link_column} = ? ORDER BY list_order", (crate_id,)
                ).fetchall()
            ]
            sets.append(SeratoSet(str(crate_name or f"crate-{crate_id}"), _asset_tracks(connection, asset_ids, warnings)))
        if include_all_local:
            sets.append(SeratoSet("Serato 本地曲目", _asset_tracks(connection, None, warnings)))
        return sets
    finally:
        connection.close()


def _location(path: str) -> str:
    return "file://localhost" + quote(str(Path(path).expanduser().resolve()))


def to_rekordbox_xml(sets: "list[SeratoSet]", out_path: str | Path) -> Path:
    """生成 rekordbox 兼容的 DJ_PLAYLISTS XML（不动任何数据库）。"""
    target = Path(out_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    root = ElementTree.Element("DJ_PLAYLISTS", {"Version": "1.0.0"})
    ElementTree.SubElement(root, "PRODUCT", {"Name": "ShadowRoom Shadow Music Bridge", "Version": "0.2.0", "Company": "ShadowRoom Music"})
    collection = ElementTree.SubElement(root, "COLLECTION")

    track_keys: "dict[str, int]" = {}
    next_key = 1
    converted = 0
    for item in sets:
        for track in item.tracks:
            if track.path in track_keys:
                continue
            if not Path(track.path).is_file():
                continue
            attributes = {
                "TrackID": str(next_key),
                "Name": track.title,
                "Artist": track.artist,
                "Kind": Path(track.path).suffix.lstrip(".").upper() + " File",
                "Size": str(Path(track.path).stat().st_size),
                "TotalTime": str(int((track.duration_ms or 0) / 1000)),
                "AverageBpm": f"{track.bpm:.2f}" if track.bpm else "",
                "DateAdded": date.today().isoformat(),
                "Location": _location(track.path),
                "Tonality": track.key or "",
            }
            element = ElementTree.SubElement(collection, "TRACK", attributes)
            if track.bpm:
                ElementTree.SubElement(
                    element,
                    "TEMPO",
                    {"Inizio": "0.024", "Bpm": f"{track.bpm:.2f}", "Metro": "4/4", "Battito": "1"},
                )
            for index, cue in enumerate(track.cues):
                if cue.in_ms is None:
                    continue
                is_loop = bool(cue.active_loop) or cue.out_ms is not None
                mark = {
                    "Name": cue.comment or "",
                    "Type": "4" if is_loop else "0",
                    "Start": f"{cue.in_ms / 1000:.3f}",
                    "End": f"{cue.out_ms / 1000:.3f}" if is_loop and cue.out_ms is not None else "-1",
                    "Num": str(index) if index < 8 else "-1",
                }
                ElementTree.SubElement(element, "POSITION_MARK", mark)
            track_keys[track.path] = next_key
            next_key += 1
            converted += 1
    collection.set("Entries", str(converted))

    playlists = ElementTree.SubElement(root, "PLAYLISTS")
    playlist_root = ElementTree.SubElement(playlists, "NODE", {"Type": "0", "Name": "ROOT", "Count": str(len(sets))})
    for item in sets:
        node = ElementTree.SubElement(
            playlist_root,
            "NODE",
            {"Name": item.name, "Type": "1", "KeyType": "0", "Entries": str(len(item.tracks))},
        )
        for track in item.tracks:
            key = track_keys.get(track.path)
            if key is not None:
                ElementTree.SubElement(node, "TRACK", {"Key": str(key)})

    ElementTree.indent(root, space="  ")
    target.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="utf-8"))
    return target


def _backup_database(database: Path) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = database.with_name(f"{database.name}.{BACKUP_SUFFIX}-{stamp}")
    shutil.copy2(database, backup)
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, backup.with_name(backup.name + suffix))
    return str(backup)


def _cue_plan(connection_query, track: SetTrack, content) -> "list[dict]":
    """算出一条曲目还需要写哪些 cue（跳过已存在的同位置 hot cue）。"""
    from pyrekordbox.db6 import tables

    existing = {
        (int(cue.InMsec or 0), int(cue.Kind or 0))
        for cue in (connection_query.query(tables.DjmdCue).filter_by(ContentID=content.ID).all() if content is not None else [])
    }
    planned: "list[dict]" = []
    for cue in track.cues:
        if cue.in_ms is None:
            continue
        is_loop = bool(cue.active_loop) or cue.out_ms is not None
        key = (int(cue.in_ms), 1)
        planned.append(
            {
                "in_ms": int(cue.in_ms),
                "out_ms": int(cue.out_ms) if is_loop and cue.out_ms is not None else None,
                "comment": str(cue.comment or ""),
                "is_loop": is_loop,
                "duplicate": key in existing,
            }
        )
        existing.add(key)
    return planned


def _beat_loop_size(track: SetTrack, cue_plan: dict) -> int:
    if not cue_plan["is_loop"] or not track.bpm or cue_plan["out_ms"] is None:
        return 0
    beat_ms = 60_000 / float(track.bpm)
    return max(1, round((cue_plan["out_ms"] - cue_plan["in_ms"]) / beat_ms))


def write_rekordbox_set(
    tracks: "list[SetTrack]",
    name: str,
    database: str | Path,
    db_dir: str | Path,
    *,
    apply: bool = False,
    backup: bool = True,
) -> dict:
    """把 Serato 曲目/cue 写进本地 Rekordbox 库（默认 dry-run）。

    ``apply=True`` 时先备份 ``master.db``，写入后重新打开数据库回读校验；Rekordbox
    必须关闭，否则 pyrekordbox 会拒绝提交。
    """
    try:
        from pyrekordbox import Rekordbox6Database
        from pyrekordbox.db6 import tables
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("writing to Rekordbox needs pyrekordbox installed") from exc
    from uuid import uuid4

    database_path = Path(database).expanduser()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    def add_cues(db, content, track: SetTrack, cue_plan: "list[dict]") -> int:
        """写 djmdCue 行 + contentCue 里的 JSON 缓存（rekordbox 两个都读）。"""
        written = 0
        new_entries: "list[dict]" = []
        now = datetime.now(timezone.utc).isoformat()
        for cue in cue_plan:
            if cue["duplicate"]:
                continue
            cue_id = db.generate_unused_id(tables.DjmdCue)
            cue_uuid = str(uuid4())
            in_msec = cue["in_ms"]
            out_msec = cue["out_ms"] if cue["out_ms"] is not None else -1
            beat_loop_size = _beat_loop_size(track, cue)
            db.add(
                tables.DjmdCue(
                    ID=cue_id,
                    ContentID=content.ID,
                    ContentUUID=content.UUID,
                    Kind=1,
                    InMsec=in_msec,
                    OutMsec=out_msec,
                    Comment=cue["comment"],
                    Color=-1,
                    ActiveLoop=1 if cue["is_loop"] else 0,
                    BeatLoopSize=beat_loop_size,
                    CueMicrosec=0,
                    UUID=cue_uuid,
                )
            )
            new_entries.append(
                {
                    "ID": cue_id,
                    "ContentID": content.ID,
                    "ContentUUID": content.UUID,
                    "InMsec": in_msec,
                    "InFrame": int(in_msec * 0.15),  # rekordbox 分析帧 = 150 fps
                    "InMpegFrame": 0,
                    "InMpegAbs": 0,
                    "OutMsec": out_msec,
                    "OutFrame": int(out_msec * 0.15) if out_msec > 0 else 0,
                    "OutMpegFrame": 0,
                    "OutMpegAbs": 0,
                    "Kind": 1,
                    "Color": -1,
                    "ActiveLoop": 1 if cue["is_loop"] else 0,
                    "Comment": cue["comment"],
                    "BeatLoopSize": beat_loop_size,
                    "CueMicrosec": 0,
                    "UUID": cue_uuid,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            written += 1
        if new_entries:
            cache = db.query(tables.ContentCue).filter_by(ContentID=content.ID).first()
            entries = []
            if cache is not None and cache.Cues:
                try:
                    entries = json.loads(cache.Cues)
                except (TypeError, ValueError):
                    entries = []
            entries.extend(new_entries)
            if cache is None:
                db.add(
                    tables.ContentCue(
                        ID=db.generate_unused_id(tables.ContentCue),
                        ContentID=content.ID,
                        Cues=json.dumps(entries, ensure_ascii=False),
                        rb_cue_count=len(entries),
                        UUID=str(uuid4()),
                    )
                )
            else:
                cache.Cues = json.dumps(entries, ensure_ascii=False)
                cache.rb_cue_count = len(entries)
        return written

    db = Rekordbox6Database(path=database_path, db_dir=db_dir, unlock=True)
    plan: "list[dict]" = []
    try:
        contents = {str(item.FolderPath): item for item in db.get_content().all()}
        playlist_exists = any(str(item.Name) == name for item in db.get_playlist().all())
        for track in tracks:
            source = Path(track.path).expanduser()
            entry: "dict" = {"id": track.id, "path": str(source), "title": track.title, "cues": len(track.cues)}
            if not source.is_file():
                entry["status"] = "missing"
                plan.append(entry)
                continue
            content = contents.get(str(source))
            entry["status"] = "existing-track" if content is not None else "new-track"
            cues = _cue_plan(db, track, content)
            entry["cues_to_add"] = sum(not cue["duplicate"] for cue in cues)
            entry["cues_skipped"] = sum(cue["duplicate"] for cue in cues)
            plan.append(entry)

        summary = {
            "schema_version": 1,
            "mode": "rekordbox-db-write",
            "set": name,
            "database": str(database_path),
            "apply": apply,
            "playlist": {"name": name, "exists": playlist_exists},
            "tracks": plan,
            "new_tracks": sum(entry["status"] == "new-track" for entry in plan),
            "existing_tracks": sum(entry["status"] == "existing-track" for entry in plan),
            "missing": sum(entry["status"] == "missing" for entry in plan),
            "cues_to_add": sum(entry.get("cues_to_add", 0) for entry in plan),
            "cues_skipped": sum(entry.get("cues_skipped", 0) for entry in plan),
        }
        if not apply:
            summary["status"] = "dry-run"
            summary["warnings"] = [
                "Dry run: nothing was written. Add apply=true (CLI: --apply) to write.",
                "Close Rekordbox before applying; master.db is backed up automatically.",
            ]
            return summary

        backup_path = _backup_database(database_path) if backup else None
        playlist = next((item for item in db.get_playlist().all() if str(item.Name) == name), None)
        if playlist is None:
            playlist = db.create_playlist(name)
        applied: "list[dict]" = []
        for track, entry in zip(tracks, plan):
            source = Path(entry["path"])
            if not source.is_file():
                continue
            content = contents.get(str(source))
            if content is None:
                metadata: "dict" = {}
                if track.title:
                    metadata["Title"] = track.title
                if track.artist:
                    existing_artist = db.get_artist(Name=track.artist).first()
                    metadata["ArtistID"] = (existing_artist or db.add_artist(track.artist)).ID
                if track.bpm:
                    metadata["BPM"] = int(round(track.bpm * 100))
                if track.duration_ms:
                    metadata["Length"] = int(round(track.duration_ms / 1000))
                content = db.add_content(source, **metadata)
                contents[str(source)] = content
            written = add_cues(db, content, track, _cue_plan(db, track, content))
            db.add_to_playlist(playlist, content)
            applied.append({"path": str(source), "content_id": str(content.ID), "cues_written": written})
        db.commit()
    except Exception as exc:
        return {
            "schema_version": 1,
            "mode": "rekordbox-db-write",
            "set": name,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "database": str(database_path),
            "warnings": ["Nothing was applied. Rekordbox must be closed; the database may be unchanged."],
        }
    finally:
        db.close()

    verify = Rekordbox6Database(path=database_path, db_dir=db_dir, unlock=True)
    try:
        playlist = next((item for item in verify.get_playlist().all() if str(item.Name) == name), None)
        playlist_tracks = len(verify.get_playlist_songs(PlaylistID=playlist.ID).all()) if playlist is not None else 0
        cue_total = 0
        for entry in applied:
            content = verify.get_content(FolderPath=entry["path"]).one_or_none()
            if content is not None:
                cue_total += verify.query(tables.DjmdCue).filter_by(ContentID=content.ID).count()
    finally:
        verify.close()

    written = sum(item["cues_written"] for item in applied)
    verified = playlist is not None and playlist_tracks >= len(applied) and cue_total >= written
    return {
        "schema_version": 1,
        "mode": "rekordbox-db-write",
        "set": name,
        "status": "verified" if verified else "verification-failed",
        "apply": True,
        "database": str(database_path),
        "backup": backup_path,
        "playlist": {"name": name, "tracks": playlist_tracks},
        "applied": applied,
        "cues_written": written,
        "cues_read_back": cue_total,
        "warnings": [
            "Open Rekordbox: the converted set should now be in your playlists with its cues.",
            f"Restore master.db from {backup_path} if anything looks wrong.",
        ],
    }
