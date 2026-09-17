# MA 能力矩阵（基于实际部署版本核验）

核验对象：部署中的 Music Assistant（`ghcr.io/music-assistant/server:latest`，镜像 id `db61b915e4fb`）。
核验方法：HTTP `/api-docs/openapi.json` + 只读 API 调用探测（2026-09-17）。
标注规则：verified = 本次有生产证据或实测；unverified = 未核验，需 WS 探针确认；
unsupported = 接口不存在。

## 1. API 表面形态（重要事实）

- HTTP `/api-docs/openapi.json` 只暴露 4 个路径：`/api`、`/auth/login`、`/auth/providers`、`/setup`。
- **播放器/队列/搜索等命令面不在 OpenAPI 里，走 websocket（`/ws`）**。
  → 「读 OpenAPI 建能力矩阵」在该部署上只能覆盖 HTTP 面；命令面必须建 WS 探针
  （IMP-03 的 `ma_reader.py` 前置工作）。
- 已核验可用的 HTTP 命令通道：`POST /api/call` + `{"command": ..., "args": {...}}`，
  Bearer `MA_LONG_TOKEN`。

## 2. 命令能力矩阵

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| `music/search`（跨 provider 搜索） | verified | HA script.music_play_query 生产使用 |
| `music/tracks/library_items`（分页读库、排序） | verified | music-fetcher 生产使用 |
| `music/sync`（触发重扫） | verified | music-fetcher 生产使用 |
| `players/all`（播放器列表） | verified | 只读探测成功；但响应形状见 §3 |
| 播放器快照（当前曲/进度/状态） | **unverified** | `players/all` 响应无 active/queue_id 字段；快照字段集需 WS 探针 |
| `player_queues/current`（队列内容） | **unverified** | 直调未成功（响应形状/参数待 WS 核对） |
| queue_id 发现 | **unverified** | players 响应中缺 queue_id 字段；需 WS |
| next / previous | unverified | — |
| play_media / 立即播放 | verified（经 HA） | script.music_play_uri 生产使用 |
| 队尾插入（QueueOption add） | verified（经 HA） | script.music_filler 生产使用 |
| 删除未来队列项 | unverified | IMP-10 前置核验 |
| 专辑展开（保持碟号/曲号顺序） | unverified | IMP-06 前置核验 |
| 收藏 / 最近播放 | unverified | IMP-09/14 前置核验 |
| 音量设置 / 静音 | unverified（HA 侧 volume_set verified） | MA 直接响度控制待核 |
| 播放器事件流（队列变化推送） | unverified | `players/all` 轮询是当前唯一已知手段 |

## 3. 播放器实况（只读快照）

| player | 类型 | 状态 |
| --- | --- | --- |
| 书房（Cast） | Google Home Mini | idle |
| Squeezebox Touch | LMS/http | playing（核验时正在播放） |

注：响应中无 `active` 布尔与 `queue_id`；「哪个是当前目标播放器」目前只能由
配置约定（IMP-03 的 player_bindings 解决）。

## 4. Providers

- 已装配 provider 集合的完整列表：unverified（WS 接口）。
- 生产证据表明至少存在：本地文件 provider（`/music`）、QQ 音乐（在线垫场 60s 试听）。

## 5. 对后续任务的影响

- IMP-03 `ma_reader.py` 的第一件事是建 **WS 探针**，把本表 unverified 行变成
  verified/unsupported；不得以官方文档最新版替代部署版实测。
- IMP-10 的「未来队列修改」在该矩阵转 verified 前保持禁用（计划风险表 R-NEW
  对应项：若 MA 无法保留当前曲，禁用能力并如实报告）。
