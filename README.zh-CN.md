# Rekordbox ⇄ Serato Bridge

一个 MCP 服务器：在 **Rekordbox 6/7** 和 **Serato DJ Pro** 之间双向搬运 DJ 资料库数据 ——
Cue、Loop、Cue 颜色和播放列表。

任何支持 MCP 的 agent / 客户端都可以直接使用。

[English](README.md) · 许可证：[AGPL-3.0](LICENSE)

## 功能

- **双向转换**：Rekordbox → Serato（生成 Serato 可直接用的文件夹 + crate）；Serato → Rekordbox
  （写进本地 Rekordbox 资料库，或导出 Rekordbox 兼容 XML）。
- **Cue / Loop / 颜色**：hot cue、memory cue、saved loop、按拍数的循环、Cue 颜色都会转换成目标软件的原生模型。
- **支持 U 盘设备库**：可以直接读 Rekordbox 设备库（`PIONEER/USBANLZ`）里的 cue/loop，不用原电脑的资料库。
- **默认只读**：不显式要求就不写任何东西；音频文件只在 staging 副本里被修改。
- **每一步都能预览**：所有写入路径都有 dry-run 和机器可读报告。

## 环境要求

| | |
| --- | --- |
| 系统 | macOS（在 Rekordbox 7 + Serato DJ Pro 4.0 上实测） |
| Python | 3.9 或更新 |
| Rekordbox | 6 或 7（Serato → Rekordbox 方向需要） |
| Serato | Serato DJ Pro 4.x |

依赖 `pyrekordbox` 会随插件一起安装。

## 安装

### 作为 Codex 插件安装

```sh
codex plugin marketplace add shadowroommusic/rekordbox-serato-bridge
codex plugin add rekordbox-serato-bridge@shadowroom
```

### 其它 MCP 客户端

```json
{
  "mcpServers": {
    "rekordbox-serato-bridge": {
      "command": "python3",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/rekordbox-serato-bridge"
    }
  }
}
```

### 只用命令行

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/shadow-rb-serato --help
```

## 配置项

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--rekordbox-database` | `~/Library/Pioneer/rekordbox/master.db` | 读写 Rekordbox 资料库 |
| `--rekordbox-dir` | `~/Library/Pioneer/rekordbox` | Rekordbox 设置与本地分析数据 |
| `--serato-database` | `~/Library/Application Support/Serato/Library/master.sqlite` | Serato crate 与曲目 |
| `--device-root` | – | Rekordbox U 盘，例如 `/Volumes/USB` |
| `--staging-dir` | – | 写入标记的副本存放目录 |

## MCP 工具

| 工具 | 作用 |
| --- | --- |
| `preview_rekordbox_to_serato` | 预览 Rekordbox → Serato 会发生什么 |
| `preview_serato_to_rekordbox` | 预览 Serato → Rekordbox 会发生什么 |
| `stage_serato_cues` | 复制一份曲目、写入 Serato cue、回读校验、生成 manifest |
| `list_sets` | 列出本地 Rekordbox 资料库里的播放列表 |
| `convert_set` | Rekordbox 的 set → Serato 可用目录（副本 + cue/loop + `_Serato_` crate） |
| `convert_set_to_rekordbox` | Serato crate → Rekordbox（dry-run / `apply` 写库 / XML 导出） |

CLI 里是同一套能力：`preview-rb-to-serato`、`preview-serato-to-rb`、`stage-cues`、
`inspect-rekordbox`、`inspect-serato`、`list-sets`、`convert-set`。

## 用法

### Rekordbox → Serato（演出文件夹 / U 盘）

```sh
# 从 Rekordbox 设备库转换（设备上的 cue 数据最权威）
.venv/bin/shadow-rb-serato convert-set --to serato --name "我的set" \
  --out ~/Music/ShadowRoom-USB --device-root /Volumes/USB --with-cues-only

# 或从本地资料库的播放列表
.venv/bin/shadow-rb-serato list-sets --rekordbox-database "$HOME/Library/Pioneer/rekordbox/master.db" \
  --rekordbox-dir "$HOME/Library/Pioneer/rekordbox"
```

输出目录就是 Serato 认识的结构：

```text
<out>/ShadowRoom/<set>/<曲目>        # 带 Serato cue/loop + BeatGrid 的副本
<out>/ShadowRoom/<set>/manifest.json
<out>/_Serato_/Subcrates/<set>.crate # Serato 侧边栏里出现的 crate
```

把整个 `<out>` 文件夹拷到 U 盘根目录即可；也可以把曲目直接导入 Serato —— 两种方式都会带上 cue。

### Serato → Rekordbox

```sh
SHADOW=.venv/bin/shadow-rb-serato
RB="$HOME/Library/Pioneer/rekordbox"

# 1) 先 dry-run
$SHADOW convert-set --to rekordbox --name "我的set" \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" \
  --rekordbox-database "$RB/master.db" --rekordbox-dir "$RB"

# 2) 确认后再写（先退出 Rekordbox；会自动备份 master.db）
$SHADOW convert-set --to rekordbox --name "我的set" --apply ...

# 不想动数据库？导出 Rekordbox 兼容 XML
$SHADOW convert-set --to rekordbox --name "我的set" --xml ~/Desktop/my-set.xml ...
```

### 预览与单曲 staging

```sh
# 两个方向的只读预览
$SHADOW preview-rb-to-serato --rekordbox-database "$RB/master.db" --rekordbox-dir "$RB" \
  --serato-database "$HOME/Library/Application Support/Serato/Library/master.sqlite" --output preview.json

# 只往 staging 副本里写 cue，并回读校验
$SHADOW stage-cues --track "/path/track.aiff" --cues cues.json --staging-dir ./staging
```

`cues.json` 可以是数组或 `{"cues": [...]}`；每项支持 `name`、`position_ms`、`end_ms`（loop）、
`color`（`#RRGGBB` 或整数）。

## 支持的音频格式

| 容器 | 读 cue | 写 cue |
| --- | --- | --- |
| MP3 | ✅ | ✅ |
| AIFF / AIFC | ✅ | ✅ |
| FLAC | ✅ | ✅ |
| WAV | ✅ | – |
| OGG | – | – |

## 安全说明

- 除非显式开启（`--apply` / `"apply": true`）或指定 staging 目录，否则全程只读。
- 写 Rekordbox 数据库前会备份 `master.db`（含 WAL/SHM），写完回读校验；Rekordbox 必须处于关闭状态，否则拒绝写入。
- 音频文件不会被原地修改 —— 新标记写进副本，报告里会证明源文件未变。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 报 `Rekordbox is running` | 退出 Rekordbox 后重试。 |
| Serato 里看不到 cue | 重新载入一次曲目（Serato 在载入时读标记），并确认 crate/文件夹已导入。 |
| 曲目显示为丢失 | 资料库指向的路径当前不存在（例如 U 盘没插）。 |
| 找不到设备曲目 | 插上 Rekordbox 设备，或改从本地资料库转换。 |

## 参与开发

见 [CONTRIBUTING.md](CONTRIBUTING.md)；实现细节（维护者文档）在 [docs/internals.md](docs/internals.md)。

## 许可证

AGPL-3.0，见 [LICENSE](LICENSE)。依赖 `pyrekordbox` 使用 MIT 许可证。
