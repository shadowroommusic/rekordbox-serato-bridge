"""Serato ↔ Rekordbox 的 cue 颜色映射。

Rekordbox 的 hot cue 颜色不是 RGB，而是**调色板编号**（``djmdCue.ColorTableIndex``）：
编号 0 = 不设颜色（rekordbox 按 pad 槽位自动给默认色），其余编号指向 rekordbox 内置的
16 色调色板。Serato 那边写的是真正的 3 字节 RGB。

下面这张表是 2026-09-14 在用户机上实测得到的：在 rekordbox GUI 的颜色菜单里逐格设色，
再读 ``djmdCue.ColorTableIndex``，和色板截图上量到的 RGB 配对（编号与菜单顺序无关，
是 rekordbox 内部的固定编号）。
"""
from __future__ import annotations

# (ColorTableIndex, 名称, RGB)
REKORDBOX_PALETTE: "tuple[tuple[int, str, int], ...]" = (
    (1, "Blue", 0x3A59F6),
    (5, "SkyBlue", 0x6BB2F9),
    (9, "Cyan", 0x66DDFB),
    (14, "Teal", 0x4EA192),
    (18, "SeaGreen", 0x51AE7B),
    (22, "Green", 0x6DDF46),
    (26, "Lime", 0xB2DF4A),
    (30, "Olive", 0xB6BE3D),
    (32, "DarkYellow", 0xC0B039),
    (38, "Orange", 0xD16B32),
    (42, "Red", 0xD33D34),
    (45, "Rose", 0xEA387B),
    (49, "Magenta", 0xCD50C9),
    (56, "Purple", 0xA63DF6),
    (60, "Violet", 0xA274F7),
    (62, "BlueViolet", 0x6773F6),
)

UNSET_COLOR_INDEX = 0
# Serato 侧"没有颜色"时写的默认色（本轮之前一直写白色，实机确认能显示）
SERATO_DEFAULT_RGB = 0xFFFFFF


def rgb_tuple(value: int) -> "tuple[int, int, int]":
    return ((int(value) >> 16) & 0xFF, (int(value) >> 8) & 0xFF, int(value) & 0xFF)


def rgb_int(red: int, green: int, blue: int) -> int:
    return (int(red) << 16) | (int(green) << 8) | int(blue)


def rgb_from_color_table_index(index: "int | None") -> "int | None":
    """rekordbox 调色板编号 → RGB 整数；0 / None / 未知编号 → None（不设颜色）。"""
    if index is None:
        return None
    try:
        wanted = int(index)
    except (TypeError, ValueError):
        return None
    if wanted == UNSET_COLOR_INDEX:
        return None
    for palette_index, _name, rgb in REKORDBOX_PALETTE:
        if palette_index == wanted:
            return rgb
    return None


def color_table_index_for_rgb(value: "int | tuple[int, int, int] | None") -> int:
    """Serato 的 RGB → 最接近的 rekordbox 调色板编号（0 = 不设颜色）。

    Serato 自己的调色板和 rekordbox 不完全一致，所以按 RGB 距离取最近的；
    白色/黑色视为"没有颜色"（我们给未设色的 cue 写的就是白色）。
    """
    if value is None:
        return UNSET_COLOR_INDEX
    if isinstance(value, int):
        value = rgb_tuple(value)
    red, green, blue = (int(value[0]), int(value[1]), int(value[2]))
    if (red, green, blue) in ((255, 255, 255), (0, 0, 0)):
        return UNSET_COLOR_INDEX
    best_index = UNSET_COLOR_INDEX
    best_distance = None
    for palette_index, _name, palette_value in REKORDBOX_PALETTE:
        pr, pg, pb = rgb_tuple(palette_value)
        distance = (pr - red) ** 2 + (pg - green) ** 2 + (pb - blue) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = palette_index
    return best_index
