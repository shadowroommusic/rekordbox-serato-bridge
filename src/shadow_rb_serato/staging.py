"""Cue 写入的 staging 流程：复制 → 写入 → 回读校验 → manifest。

这里永远不会写原始音频文件、Rekordbox 数据库或 Serato 数据库；所有改动只发生在
``staging_dir`` 里的副本上，写完立刻用同一个读取器回读校验，并把结果记进 manifest，
方便人工确认后再决定要不要真正应用。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from . import serato_markers
from .serato_markers import MARKERS2_DESC

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1


def _staged_path(staging: Path, source: Path) -> Path:
    target = staging / source.name
    if not target.exists():
        return target
    return staging / f"{source.stem}-staged{source.suffix}"


def _marker_signature(markers: "list[serato_markers.SeratoMarker]") -> "list[tuple]":
    return [(marker.kind, marker.position_ms, marker.end_ms, marker.name) for marker in markers]


def _update_manifest(staging: Path, entry: dict) -> Path:
    manifest_path = staging / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}
    payload.setdefault("schema_version", MANIFEST_SCHEMA_VERSION)
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload.setdefault("entries", []).append(entry)
    payload["entry_count"] = len(payload["entries"])
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def stage_serato_cues(track_path: str | Path, cues, staging_dir: str | Path) -> dict:
    """把 cues 写成 Serato Markers2，只写进 staging 副本并回读校验。"""
    source = Path(track_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"not a file: {source}")

    markers = serato_markers.from_cue_points(list(cues))
    if not markers:
        raise ValueError("no cues to stage")
    for marker in markers:
        if marker.position_ms is None:
            raise ValueError("every cue needs a position_ms value")
        if marker.kind == "loop" and marker.end_ms is None:
            raise ValueError("a loop cue needs end_ms")

    staging = Path(staging_dir).expanduser()
    staging.mkdir(parents=True, exist_ok=True)
    original_bytes = source.read_bytes()
    staged = _staged_path(staging, source)
    shutil.copy2(source, staged)

    report = {
        "schema_version": 1,
        "mode": "staging-write",
        "source": str(source),
        "staged": str(staged),
        "staging_dir": str(staging),
        "markers_requested": len(markers),
    }
    try:
        blob = serato_markers.build_markers2(markers)
        serato_markers.replace_geob_tags(staged, {MARKERS2_DESC: blob})
        read_back, marker_source = serato_markers.read_markers(staged)
    except Exception as exc:
        report.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        report["manifest"] = str(_update_manifest(staging, report))
        return report

    expected = _marker_signature(markers)
    actual = _marker_signature(read_back)
    verified = expected == actual
    report.update(
        {
            "status": "verified" if verified else "verification-failed",
            "marker_source": marker_source,
            "markers_read_back": len(read_back),
            "verification": {"expected": expected, "actual": actual, "matches": verified},
            "source_unchanged": source.read_bytes() == original_bytes,
            "warnings": [
                "Only the staged copy was modified; the original audio file and both vendor databases were left untouched.",
                "Nothing is written to Rekordbox or Serato by this step; reviewed staging output must be applied by the user.",
            ],
        }
    )
    report["manifest"] = str(_update_manifest(staging, report))
    return report
