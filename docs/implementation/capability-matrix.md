# MA 能力矩阵（基于实际部署版本核验）

核验对象：部署中的 Music Assistant（`ghcr.io/music-assistant/server:latest`，镜像 id `db61b915e4fb`）。
核验方法与记录：
- 2026-09-17 IMP-00：HTTP `/api-docs` + 只读 API 探测（首次）
- 2026-09-17 IMP-03b：`tools/ma_capability_probe.py` HTTP 命令内省（第二轮，
  含真实 player_id）+ WS 连接/事件观察

标注规则：verified = 本次有生产证据或实测；supported-validation = 命令存在
（以参数校验错误证实），功能性待实机；unverified = 未核验。

## 1. API 表面形态（实测）

- HTTP `/api-docs/openapi.json` 仅 4 路径：`/api`、`/auth/login`、
  `/auth/providers`、`/setup`。命令面经 `POST /api`（`{"command","args"}`）
  与 websocket 推送；官方文档确认 message_id 回显机制。
- **部署版 players/all 响应不含 `active` / `queue_id` 字段**（IMP-00/baseline）；
  **含** `volume_level`、`playback_state`、`current_media`、`available`、
  `elapsed_time`、`supported_features`、`sleep_timer_expires_at`、
  `mute_control`、`volume_muted`（IMP-03b 实测全键清单）。

## 2. 命令能力矩阵（IMP-03b 实测更新）

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| `music/search`（跨 provider） | verified | 生产使用 + 探针复核（「晴天」3 条） |
| `music/tracks/library_items` | verified | fetcher 生产使用 |
| `music/sync` | verified | fetcher 生产使用 |
| `players/all` | verified | 探针：2 players，含 volume_level |
| **播放器音量读取（volume_level）** | **verified** | 实测 82；players 键清单含 volume_level |
| **queue_id 发现**（get_active_queue{player_id}） | **verified** | 实测返回 queue_id（部署版 queue_id==player_id） |
| **队列内容**（player_queues/items） | **verified** | 实测 5 items |
| **最近播放**（music/recently_played_items） | **verified** | 实测返回 QQ provider 真实条目 |
| **队列删除**（player_queues/delete） | supported-validation | 假 queue_id → 400 参数校验（命令存在） |
| **专辑展开**（music/album_tracks） | supported-validation | 同上（400 = 命令存在） |
| **收藏**（music/add_to_favorites） | supported-validation | 同上（400 = 命令存在） |
| next / previous | supported-validation | 缺 queue_id → 500 参数错误（非 Unknown command）；功能待实机 |
| 音量写（players/cmd/volume_set） | supported-validation | 缺 player_id → 500 参数错误 |
| 事件流（WS 推送） | verified（播放期间） | 播放中 WS 持续推送事件（首版探针被事件流阻塞即证据）；事件分类待 IMP-03c |

**unverified 清零**（9 项全部转为 verified / supported-validation）。
功能性（真实变更）核验随 IMP-03c ha_executor 与 IMP-08 实机验收继续。

## 3. 播放器实况（IMP-03b 探针时点）

| player_id | 名称 | 状态 | volume_level |
| --- | --- | --- | --- |
| `3085eefe-b6ce-cd4e-7184-591c5e6605d5` | Squeezebox Touch（sq 类型） | idle（当时） | 82 |
| （Cast） | study-cast | idle | 50 |

注意：`get_active_queue` 返回的 `queue_id == player_id`（部署版约定）；
队列 `current_item.uri` 可能为 null（MA 对本地文件的行为），应以 `name` 兜底。

## 4. 对后续任务的影响

- IMP-03b `playback_state.py`：以实测字段（playback_state/volume_level/
  current_media/queue_id）构建快照 ✓ 已实现。
- IMP-03c：写命令（next/previous/volume/play_media）经 HA
  `script.music_execute_v1` 下发（D13），命令存在性已由本表确认。
- IMP-10：未来队列删除 supported-validation → 「保留当前曲，只换后面」
  的能力前提成立，功能验收在 IMP-10。
