# IMP-00 PRECHECK 报告（只读核验）

- 任务：IMP-00（只读 PRECHECK、能力盘点与基线登记）
- 执行日期：2026-09-17
- 仓库基线：`main@31ad4368fef4d9f5f5c579aa0e9e4f0ef678d86d`；实际 HEAD 与基线**一致**，无未提交漂移
- 方法：仓库静态阅读 + 对部署主机（下称 VM1）的只读 SSH 核验 + MA HTTP API 只读探测
- 纪律：未重启/修改任何容器、未改端口/挂载、未发送播放或下载命令、未保存凭据

## 1. 仓库状态

- 分支 `main`，HEAD `31ad436`，工作区干净；与计划核验基线一致，无先行提交需要登记。
- 输入文件 `home_music_agent_review_31ad436.md` 在位（内部库 `docs/internal/`）。
- 本仓库（开源版）与内部工作库的对应关系：工作库含内部文档与真实配置，开源库为脱敏子集。

## 2. 部署现场与文档的差异（重点漂移）

| 项 | 开源仓库描述 | 实际部署 | 结论 |
| --- | --- | --- | --- |
| 容器数量 | 5 个（ha/ma/stt/gateway/fetcher） | **7 个**：另有 `home-music-pwa`（python:3.12-slim，静态 PWA，端口 8400）与 `home-music-proxy`（caddy:2-alpine，HTTPS 入口） | compose.yaml 缺 pwa/proxy 定义，需回填 |
| HTTPS 入口 | 「没有则加轻量代理」 | **已存在**：caddy 终结 TLS，`/gw/*→:8200`、`/stt/*→:8100`、默认→PWA，body 上限 20MB | IMP-04 的入口条件已满足一半，需核验证书链与信任 |
| 部署目录版本管理 | 文档暗示可复现 | `/opt/home-music-agent` **不是 git 仓库**，无法核验现场文件与仓库的一致性 | 登记为风险 R-NEW-01 |
| HA/MA 镜像 tag | 固定版本承诺 | `home-assistant:stable`、`music-assistant/server:latest`（浮动 tag） | 登记为风险 R-NEW-02 |
| 根磁盘 | — | 使用率 81%（剩余 19G） | 容量预算需纳入 IMP-04 |

## 3. 运行组件盘点（7 容器）

| 容器 | 镜像 | 端口 | 关键挂载 |
| --- | --- | --- | --- |
| home-assistant | ghcr.io/home-assistant/home-assistant:stable | 8123（0.0.0.0） | config/home-assistant → /config |
| music-assistant | ghcr.io/music-assistant/server:latest | 8095 | config → /data；/mnt/music → /music (rw) |
| stt-service | 本地构建 | 8100 | config(ro)、transcripts(rw)、models(ro) |
| voice-gateway | 本地构建 | 8200 | data/sessions(rw)、.env(ro) |
| music-fetcher | 本地构建 | 8300 | .env(ro)、sessions(ro)、fetcher data(rw)、/mnt/music(rw) |
| home-music-pwa | python:3.12-slim | 8400 | services/pwa/www |
| home-music-proxy | caddy:2-alpine | 443 类（见 Caddyfile） | Caddyfile + ssl/ |

## 4. 存储、模型与 NAS

- 曲库：NFS v3（soft/tcp）挂载 `/mnt/music`，服务端 7.3T、已用 2.4T（33%）；读写核验通过（MA 历史入库正常）。
- STT：SenseVoice ONNX（`model.onnx` + `model.int8.onnx` + `tokens.txt`）在 `data/models/sense-voice/`，容器只读挂载 ✓。
- 会话/画像数据：`data/sessions/*.jsonl`（gateway 写、fetcher 只读挂载）——即计划 §4.6 中要被 SQLite 逐步替代的「累计快照」。
- VM 根分区 81% 使用：迁移与备份前需容量评估。

## 5. 写播放状态的路径盘点

| 写路径 | 状态 |
| --- | --- |
| voice-gateway `/command`、`/agent`（→ HA script） | 现役主路径（生产验证） |
| HA `script.music_play_query / music_play_uri / music_filler / music_volume_set` | 现役（logbook 有触发记录） |
| music-fetcher 完成回调 → HA `music_play_uri` | 存在（即 IMP-02 要阻断的自动抢播路径） |
| 每日自动充实 automation（03:30，HA 触发 fetcher） | 存在 |
| 物理遥控（Squeezebox 遥控/机身） | 存在，属外部人工控制 |
| MA 自带 Web UI | 存在，属外部人工控制 |
| PWA（8400） | 待核验其是否含控制写路径（此前未审查其 app.js） |

## 6. 未核验项（unverified）

- MA 命令面细节：部署版 HTTP `/api-docs` 仅暴露 `api/auth/info/setup` 四个路径；播放器/队列/搜索等命令面走 websocket。以下命令在生产中有成功证据：`music/search`、`music/tracks/library_items`、`music/sync`、`players/all`。`player_queues/current`、next/previous、未来队列删除、专辑展开、收藏、最近播放、响度控制：**unverified**，需 IMP-03 建 WS 探针。
- MA 播放器 API 响应中未见 `active` / `queue_id` 字段（该部署版本返回形状如此），队列发现需经 WS 或 players 详情接口再核。
- PWA `app.js`/`sw.js` 的能力与缓存策略未审查（IMP-05/07 前置）。
- 手机端版本与入口、语音客户端版本：未采集（本机外设备）。
- Caddy 证书链的受信状态（浏览器是否信任）未验证。
- `.env` 值未读取（仅核对键名齐全，14 键与 .env.example 对应）。

## 7. 结论

PRECHECK 通过：仓库与基线一致；现场可完整盘点；发现 5 项漂移/风险已登记
（详见 `risk-register.md`）。IMP-01（回归测试与契约样例）的前置条件已满足。
