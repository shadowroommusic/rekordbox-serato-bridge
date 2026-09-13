# Rekordbox Serato Bridge

ShadowRoom Music 出品（Shadow Producers 工具集）。读 Rekordbox 6/7 与 Serato DJ Pro 的资料库，把曲目与 Cue 点统一成一套模型，输出两个方向的可迁移字段与差异报告；**默认只读**，写入只发生在 staging 副本里。

This ShadowRoom Music plugin reads Rekordbox 6/7 (through `pyrekordbox`) and Serato DJ Pro's `master.sqlite` in read-only mode, normalizes tracks and cue points, matches local assets, and reports what can be migrated in either direction. Vendor databases and audio files are never edited.

## Cue 点是怎么读到的

| 来源 | 位置 | 本插件怎么读 |
| --- | --- | --- |
| Rekordbox | `master.db` 的 `djmdCue` 表（memory cue / hot cue / loop） | `pyrekordbox`，只读加锁读取 |
| Serato DJ Pro 4.x | **不在** `master.sqlite` 里：cue/loop 写在音频文件的 ID3 GEOB 帧 `Serato Markers2`（或旧版 `Serato Markers_`） | 零依赖直接解析 ID3（MP3 开头 / AIFF `ID3 ` chunk / WAV `id3 ` chunk），v2 与 v1 两种格式都支持 |

Serato 本地文件的真实路径来自 `asset.portable_id`（相对卷根目录），插件的 `read_serato` 会据此定位文件并读取标记；读不到时会在报告里明确写出原因（文件不在、没有标签、容器不支持）。

## 三种用法

```sh
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .

# 1) 双向只读预览
.venv/bin/shadow-rb-serato preview-rb-to-serato \
  --rekordbox-database "$HOME/Library/Pioneer/rekordbox/master.db" \
  --rekordbox-dir "$HOME/Library/Pioneer/rekordbox" \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" \
  --output preview.json
.venv/bin/shadow-rb-serato preview-serato-to-rb --serato-database ... --rekordbox-database ... --rekordbox-dir ...

# 2) 查看资料库概况（曲目、Cue、非本地音源）
.venv/bin/shadow-rb-serato inspect-rekordbox --database ... --db-dir ... --output rb.json
.venv/bin/shadow-rb-serato inspect-serato --database ... --output serato.json

# 3) 把 cue 写进 staging 副本（Serato Markers2），写完立刻回读校验
.venv/bin/shadow-rb-serato stage-cues --track "/path/track.aiff" --cues cues.json --staging-dir ./staging
```

`cues.json` 可以是数组，也可以是 `{"cues": [...]}`；每项支持 `name`、`position_ms`、`end_ms`（loop）、`color`（`#RRGGBB` 或整数）。

## 报告里有什么

- 三级匹配：绝对路径 → 文件名+大小 → 元数据（标题+艺术家），每条都带置信度。
- Cue 对比：源 cue 数、目标已有 cue 数；两边数量不一致时明确提示「合并还是替换」。
- 差异与警告：时长冲突、流媒体音源不可验证、未匹配曲目、目标库里未被引用的本地文件。
- Serato 侧新增统计：`local_assets_with_cues`、`cue_count`（本地资产）。

## 写入安全（staging）

`stage-cues` 与 MCP 工具 `stage_serato_cues` 只做四件事：**复制 → 写入 → 回读校验 → 记 manifest**。

- 只改 `staging_dir` 里的副本，原始音频、Rekordbox 数据库、Serato 数据库都不动（报告里的 `source_unchanged` 会验证这一点）。
- 回读用的是同一个读取器，`verification.expected == verification.actual` 才算 `verified`。
- `manifest.json` 记录每次写入的来源、产物、校验结果，可人工复核后再决定是否真正应用。
- 目前写入只支持 MP3 与 AIFF（容器结构明确）；WAV 可读 cue 但拒绝写入，其它容器直接报错，绝不写坏文件。

## MCP

`.mcp.json` 暴露三个工具：

| 工具 | 作用 |
| --- | --- |
| `preview_rekordbox_to_serato` | Rekordbox → Serato 只读预览 |
| `preview_serato_to_rekordbox` | Serato → Rekordbox 只读预览 |
| `stage_serato_cues` | 把 cue 写进 staging 副本并回读校验（`track`、`cues`、`staging_dir`） |

## 已验证 / 未做

- 已验证：真实 Rekordbox 库（115 曲 / 80 首带 cue）与真实 Serato 库（8426 资产）双向预览；真实 rekordbox cue 写进 AIFF staging 副本并回读一致；Markers2 与我们写入的数据用独立实现（`serato-tools`）交叉校验通过。
- 未做：FLAC/OGG 等容器的标记读写；Serato BeatGrid 迁移；直接写入 Serato/Rekordbox 正式库（需要人工确认流程与备份）。

## License

MIT for this plugin. `pyrekordbox` is MIT.
