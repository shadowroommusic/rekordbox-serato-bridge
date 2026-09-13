# Rekordbox Serato Bridge

ShadowRoom Music 出品（Shadow Producers 工具集）。读 Rekordbox 6/7 与 Serato DJ Pro 的资料库，把曲目与 Cue 点统一成一套模型，输出两个方向的可迁移字段与差异报告；**默认只读**，写入只发生在 staging 副本里。

This ShadowRoom Music plugin reads Rekordbox 6/7 (through `pyrekordbox`) and Serato DJ Pro's `master.sqlite` in read-only mode, normalizes tracks and cue points, matches local assets, and reports what can be migrated in either direction. Vendor databases and audio files are never edited.

## Cue 点是怎么读到的

| 来源 | 位置 | 本插件怎么读 |
| --- | --- | --- |
| Rekordbox | `master.db` 的 `djmdCue` 表（memory cue / hot cue / loop） | `pyrekordbox`，只读加锁读取 |
| Serato DJ Pro 4.x | **不在** `master.sqlite` 里：cue/loop 写在音频文件的 ID3 GEOB 帧 `Serato Markers2`（或旧版 `Serato Markers_`） | 零依赖直接解析 ID3（MP3 开头 / AIFF `ID3 ` chunk / WAV `id3 ` chunk），v2 与 v1 两种格式都支持 |

Serato 本地文件的真实路径来自 `asset.portable_id`（相对卷根目录），插件的 `read_serato` 会据此定位文件并读取标记；读不到时会在报告里明确写出原因（文件不在、没有标签、容器不支持）。

两个实测细节：

- **热 cue 和 saved loop 是两套独立槽位**（CUE 条目 / LOOP 条目各自的 `index`，0 = 第 1 个），写 tag 时不能共用一个计数器，否则 loop 后面的 cue 会串位；LOOP 条目的字节布局固定为 `00 | index | start(4) | end(4) | FF FF FF FF | 00 | R | G | B | 00 | locked | label`（对齐 Serato 官方实现，已用 Serato 4.0.0 实机确认能读出来）。
- Serato 库的 `asset.bpm` 经常是空的，BPM 兜底从文件里的 `Serato BeatGrid` 帧读（`parse_beatgrid_bpm`）；crate 的曲目在 Serato 4.x 里走 `container → location_container → container_asset → asset`，旧版本直接挂在 crate id 上，两种结构都支持。

## 两个转换方向

| 方向 | 命令 | 输入 |
| --- | --- | --- |
| **Rekordbox → Serato** | `convert-set --to serato` | 本地库的播放列表，或 rekordbox U 盘设备库（读 USBANLZ 里的 cue/loop） |
| **Serato → Rekordbox** | `convert-set --to rekordbox` | Serato 的 crate / 本地曲目（cue 从音频文件的 Markers2 读取），写进本地 Rekordbox 库或导出 XML |

两个方向都是**只写副本**：原始音频、Rekordbox 库、Serato 库都不会被改动（反向写 Rekordbox 库时需要你显式 `--apply`，届时自动备份 `master.db`）。

### Rekordbox → Serato（场地只有 Serato）

```sh
# 用 rekordbox U 盘里的设备库（推荐：设备上的 cue/loop 是权威数据）
.venv/bin/shadow-rb-serato convert-set \
  --to serato --name "测试" --out ~/Music/ShadowRoom-USB \
  --device-root /Volumes/f379pro \
  --track "/Volumes/f379pro/1/a.mp3" --track "/Volumes/f379pro/1/b.mp3"

# 或者用本地库里的播放列表（set）
.venv/bin/shadow-rb-serato list-sets --rekordbox-database ... --rekordbox-dir ...
.venv/bin/shadow-rb-serato convert-set --to serato --name "我的set" --out ~/Music/ShadowRoom-USB \
  --rekordbox-database ... --rekordbox-dir ... --playlist "我的set"
```

输出结构（**整个文件夹拷到 U 盘根目录**即可）：

```text
<out>/ShadowRoom/<set 名>/a.mp3        # 带 Serato Markers2(cue/loop) + BeatGrid 的副本
<out>/ShadowRoom/<set 名>/manifest.json
<out>/_Serato_/Subcrates/<set 名>.crate # Serato 播放列表
```

到现场：U 盘插上 Serato，crate 里的曲目带着 A/B（以及 loop）hot cue；若 Serato 没有自动显示该 crate，把音乐拖进库里也会带上同样的 cue。

### Serato → Rekordbox（场地只有 rekordbox）

```sh
# 先看会做什么（不写库）
.venv/bin/shadow-rb-serato convert-set --to rekordbox --name "测试" \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" \
  --rekordbox-database "$HOME/Library/Pioneer/rekordbox/master.db" \
  --rekordbox-dir "$HOME/Library/Pioneer/rekordbox"

# 确认无误后写入（Rekordbox 必须关闭；自动备份 master.db + 回读校验）
.venv/bin/shadow-rb-serato convert-set --to rekordbox --name "测试" --apply ...

# 不想动数据库？导出 rekordbox 兼容 XML，在 Rekordbox 里导入
.venv/bin/shadow-rb-serato convert-set --to rekordbox --name "测试" --xml ~/Desktop/测试.xml ...
```

写库会同时更新 `djmdCue` 明细行和 `contentCue` 的 JSON 缓存（rekordbox 两者都读），并创建同名播放列表；完成后重新打开 Rekordbox 就能看到。

写入 cue 时用的是 rekordbox 自己的存储模型（2026-09-14 用 16 条对照 cue 逐 pad 实机核对，另外用 rekordbox 自己的 XML 导出核对过 `POSITION_MARK Num` / `Type`）：

| 字段 | 含义 |
| --- | --- |
| `djmdCue.Kind = 0` | memory cue（波形上的标记，不占 pad），memory 也可以是 loop |
| `djmdCue.Kind = 1,2,3` | pad A、B、C |
| `djmdCue.Kind = 4` | **rekordbox 不显示**（实测写了 2 条，界面上都找不到），必须避开 |
| `djmdCue.Kind = 5..17` | pad D、E … P（共 16 个 pad） |
| `djmdCue.OutMsec >= 0` | 这是一条 **loop**（rekordbox XML 里就是 `Type="4"` 且带 `End`），rekordbox 界面显示为黄色循环图标 |
| `djmdCue.BeatLoopSize` | 节拍循环的拍数，打包成 `(拍数 << 16) \| 1`（实测 4 拍 = 262145 = 0x40001、16 拍 = 1048577）；`0` 表示任意长度循环（实测手动 1.5s loop 就是这个写法） |

**pad 字母只由 `Kind` 决定**（和 cue 在曲中的位置、行 ID 顺序都无关），所以写入规则是：第 n 条 cue 用 `PAD_KINDS[n]`，其中 `PAD_KINDS = (1,2,3,5,6,…,17)` —— 第 4 个 pad（D）必须写 5。loop 额外填 `OutMsec` 和 `BeatLoopSize`（不对拍的 loop 写 0，保留精确 In/Out）；超过 16 条时降级为 memory cue 保留数据。实机验证结果（rekordbox 自己的导出）：

```
CONTEXT :
  POSITION_MARK Name="A" Type="0" Start="0.079"  Num="0"                 ← pad A
  POSITION_MARK Name="B" Type="0" Start="27.079" Num="1"                 ← pad B
  POSITION_MARK Name=""  Type="4" Start="61.364" End="63.079" Num="2"    ← pad C，4 拍 loop
```

反向（Serato → Rekordbox）同样保留 loop：Serato 的 `LOOP` 标记写出 `OutMsec` + `BeatLoopSize`（按 BPM 换算拍数，Serato 库里没 BPM 时从文件的 Serato BeatGrid 兜底读），回到 rekordbox 就是一条黄色循环区；如果 loop 不是整拍，就写 `BeatLoopSize=0`，精确的 In/Out 仍然保留。

## 预览与单曲 staging

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

`.mcp.json` 暴露六个工具：

| 工具 | 作用 |
| --- | --- |
| `preview_rekordbox_to_serato` | Rekordbox → Serato 只读预览 |
| `preview_serato_to_rekordbox` | Serato → Rekordbox 只读预览 |
| `stage_serato_cues` | 把 cue 写进 staging 副本并回读校验（`track`、`cues`、`staging_dir`） |
| `list_sets` | 列出本地 Rekordbox 库里的播放列表（set） |
| `convert_set` | Rekordbox set（本地库或 U 盘设备库）→ Serato 可用目录（副本 + cue/loop + gig 用 crate） |
| `convert_set_to_rekordbox` | Serato crate/本地曲目 → Rekordbox（dry-run / `apply` 写库 / XML 导出） |

## 已验证 / 未做

- 已验证：真实 Rekordbox 库与真实 Serato 库双向预览；设备 ANLZ 里的 cue/loop 读出并转成 Serato Markers2 + BeatGrid；Markers2 用独立实现（`serato-tools`）交叉校验通过；Serato 标记 → Rekordbox 库（djmdCue + contentCue 缓存 + 播放列表）写入并回读一致。
- 2026-09-14 实机闭环（Rekordbox 7 + Serato DJ Pro 4.0.0，用户机上跑通）：
  - 用 16 条对照 cue 逐 pad 核对出 pad A..P 的 `Kind` = (1,2,3,5,6,…,17)，**Kind=4 在 rekordbox 里不显示**（写它等于丢 cue）；
  - 4 拍 / 16 拍 / 手动画的 1.5 秒 loop 都验证了 `OutMsec` + `BeatLoopSize`（整拍用 `(拍数<<16)|1`，非整拍写 0）；
  - Rekordbox 的 4 拍 loop → Serato：Serato 的 saved loops 列表里正常出现（`01:01.4`，可加载播放）；
  - 反向：Serato 文件 → Rekordbox，自动写成 pad C 的黄色 4 拍 loop（`BeatLoopSize=262145`）。
- 未做：FLAC/OGG 等容器的标记读写；Serato BeatGrid → Rekordbox 网格（目前只用它取 BPM）；cue 颜色映射（Rekordbox 颜色表与 Serato 调色板不同，目前 Color 留空）；Serato 侧"用户自己保存的 loop"样本对照（本轮用的是我们写进去的 loop，Serato 与 Rekordbox 都确认能读）。

## License

MIT for this plugin. `pyrekordbox` is MIT.
