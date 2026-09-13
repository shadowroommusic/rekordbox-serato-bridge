from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# pad A..P 对应的 djmdCue.Kind（2026-09-14 实机实测：16 条对照 cue 逐 pad 核对）。
# Kind=4 在 rekordbox 界面里不显示（写了也找不到），所以第 4 个 pad（D）用 5。
PAD_KINDS = (1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)


def pad_rank(kind: int) -> int:
    """Kind → pad 顺序（0=A…15=P）；memory（0）和未知值排到最后。"""
    try:
        return PAD_KINDS.index(int(kind))
    except ValueError:
        return len(PAD_KINDS)


@dataclass(frozen=True)
class CuePoint:
    kind: int
    in_ms: int | None
    out_ms: int | None
    comment: str | None
    color: int | None
    active_loop: bool | None
    beat_loop_size: int | None


@dataclass(frozen=True)
class Track:
    source_id: str
    title: str
    artist: str
    path: str
    file_name: str
    size: int | None
    length_ms: int | None
    bpm: float | None
    key: str | None
    rating: int | float | None
    color: int | str | None
    cues: tuple[CuePoint, ...] = field(default_factory=tuple)
    source_kind: str = "local"
    # rekordbox 分析出来的第一拍位置（毫秒），用来给 Serato BeatGrid 当锚点
    beat_anchor_ms: int | None = None


def to_json(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_json(item) for key, item in asdict(value).items()}
    if isinstance(value, (tuple, list)):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    return value
