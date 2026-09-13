from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import unicodedata

from .model import Track


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


@dataclass(frozen=True)
class Match:
    source_id: str
    target_id: str | None
    method: str
    confidence: float
    cue_count: int
    target_cue_count: int
    source_path: str
    target_path: str | None
    warnings: tuple[str, ...]


def preview(source_name: str, source: list[Track], target_name: str, target: list[Track]) -> dict:
    """Create a directional preview; the same function is used in both directions."""
    local_target = [track for track in target if track.source_kind == "local"]
    by_path = {_norm(track.path): track for track in local_target if track.path}
    by_file: dict[tuple[str, int | None], list[Track]] = {}
    by_title: dict[tuple[str, str], list[Track]] = {}
    for track in local_target:
        by_file.setdefault((_norm(track.file_name), track.size), []).append(track)
        by_title.setdefault((_norm(track.title), _norm(track.artist)), []).append(track)

    matches: list[Match] = []
    for item in source:
        warnings: list[str] = []
        found: Track | None = None
        method = "unmatched"
        confidence = 0.0
        if item.source_kind != "local":
            warnings.append(f"source is {item.source_kind}; audio is not locally verifiable")
        # Streaming and placeholder identifiers cannot receive local target markers.
        if item.source_kind != "local":
            candidates = []
        elif item.path and _norm(item.path) in by_path:
            found, method, confidence = by_path[_norm(item.path)], "exact-path", 1.0
            candidates = []
        elif item.file_name:
            candidates = by_file.get((_norm(item.file_name), item.size), [])
            if len(candidates) == 1:
                found, method, confidence = candidates[0], "filename-size", 0.94
            elif len(candidates) > 1:
                warnings.append("multiple target files share the same filename and size")
        if found is None and item.source_kind == "local":
            candidates = by_title.get((_norm(item.title), _norm(item.artist)), [])
            if len(candidates) == 1:
                found, method, confidence = candidates[0], "metadata", 0.72
            elif len(candidates) > 1:
                warnings.append("multiple target assets match title and artist")
        if found is not None and item.length_ms and found.length_ms:
            delta = abs(item.length_ms - found.length_ms)
            if delta > 2000:
                warnings.append(f"duration differs by {delta} ms; Cue coordinates need review")
                confidence = min(confidence, 0.45)
        if found is not None and item.cues and found.cues and len(item.cues) != len(found.cues):
            warnings.append(
                f"target already has {len(found.cues)} cue(s) while the source has {len(item.cues)}; "
                "decide whether to merge or replace before writing"
            )
        elif found is not None and found.cues and not item.cues:
            warnings.append(f"target has {len(found.cues)} cue(s) the source does not have")
        if not item.cues:
            warnings.append("source track has no Cue points")
        matches.append(
            Match(
                item.source_id,
                found.source_id if found else None,
                method,
                confidence,
                len(item.cues),
                len(found.cues) if found else 0,
                item.path,
                found.path if found else None,
                tuple(warnings),
            )
        )

    matched_ids = {match.target_id for match in matches if match.target_id}
    return {
        "schema_version": 1,
        "mode": "read-only-preview",
        "direction": f"{source_name}-to-{target_name}",
        "source": {"track_count": len(source), "tracks_with_cues": sum(bool(item.cues) for item in source), "cue_count": sum(len(item.cues) for item in source), "non_local_tracks": sum(item.source_kind != "local" for item in source)},
        "target": {
            "asset_count": len(target),
            "local_asset_count": len(local_target),
            "streaming_asset_count": sum(item.source_kind == "streaming" for item in target),
            "local_assets_with_cues": sum(bool(item.cues) for item in local_target),
            "cue_count": sum(len(item.cues) for item in local_target),
        },
        "matches": [asdict(match) for match in matches],
        "summary": {"exact_path": sum(item.method == "exact-path" for item in matches), "filename_size": sum(item.method == "filename-size" for item in matches), "metadata": sum(item.method == "metadata" for item in matches), "unmatched": sum(item.target_id is None for item in matches), "matched_with_cues": sum(item.target_id is not None and item.cue_count > 0 for item in matches), "target_assets_not_referenced": len(local_target) - len(matched_ids)},
        "warnings": ["No vendor database or audio file was modified.", "A match does not prove Cue equivalence; a write implementation must re-read target markers and compare coordinates.", "Streaming assets cannot receive local target Cue markers."],
    }
