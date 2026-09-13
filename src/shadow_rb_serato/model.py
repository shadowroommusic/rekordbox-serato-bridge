from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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


def to_json(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_json(item) for key, item in asdict(value).items()}
    if isinstance(value, (tuple, list)):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    return value
