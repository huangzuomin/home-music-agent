# 部署清单（deployment inventory）

核验时间：2026-09-17。仅记录只读核验所得；IP 按开源约定使用 192.168.1.x 段占位。

## 主机与网络

- VM1：Ubuntu，Docker 部署宿主；SSH 别名见内部文档（不入公开库）。
- 开放端口（0.0.0.0 监听）：8123（HA）、8095（MA）、8100（STT）、8200（gateway）、8300（fetcher）；
  另有 PWA 8400、caddy 443 类入口。
- HTTPS 入口：caddy 终结 TLS（域名占位 `<domain>`），路由 `/gw/*→gateway`、`/stt/*→STT`、
  默认→PWA；`request_body max_size 20MB`（音频上传配额已存在）。
- 根分区使用率 81%（剩余 19G）；NAS 曲库卷 7.3T 已用 33%。

## 容器（7 个，运行 2~5 天）

| 容器 | 镜像 | 备注 |
| --- | --- | --- |
| home-assistant | ghcr.io/home-assistant/home-assistant:stable | 浮动 tag |
| music-assistant | ghcr.io/music-assistant/server:latest | 浮动 tag |
| stt-service | home-music-agent-stt-service（本地构建） | SenseVoice ONNX |
| voice-gateway | home-music-agent-voice-gateway（本地构建） | 单写进程前提 |
| music-fetcher | home-music-agent-music-fetcher（本地构建） | 资源准备器 |
| home-music-pwa | python:3.12-slim | 静态 PWA 服务（www/） |
| home-music-proxy | caddy:2-alpine | HTTPS 同源入口 |

（另有一个无关 postgres 容器 `qm-dev-pg-*`，非本项目，登记避免误伤。）

## 挂载（与数据归属）

| 容器 | 挂载 |
| --- | --- |
| home-assistant | config/home-assistant → /config |
| music-assistant | config/music-assistant → /data；/mnt/music → /music (rw) |
| stt-service | config/stt (ro)；data/transcripts (rw)；data/models (ro) |
| voice-gateway | data/sessions (rw)；.env (ro) |
| music-fetcher | .env (ro)；data/sessions (ro)；services/music-fetcher/data (rw)；/mnt/music (rw) |

NAS：NFS v3 soft/tcp，`nas:/volume4/music → /mnt/music`（rw），7.3T/已用 2.4T。

## 模型与数据

- STT：SenseVoice ONNX（model.onnx / model.int8.onnx / tokens.txt / test_wavs）。
- 会话与画像：`data/sessions/*.jsonl`（agent-sessions / track-history / voice-gateway 日志）。
- 补库数据：`services/music-fetcher/data/`（jobs.jsonl、reject/ 等）。

## 配置文件

- `/opt/home-music-agent/.env`：17 个键（HA/MA 凭据与 URL、STT_URL、LLM 三元组、
  智谱备用三元组、HA 令牌三元组）；键名与 `baseline-manifest.json` 的
  `env_keys` 对应；**值未读取**。
- HA 包：`config/home-assistant/packages/home_music_agent.yaml`（单文件包，在位）。
- Caddyfile：`services/https-proxy/Caddyfile` + `ssl/`。
- 部署目录 `/opt/home-music-agent` **未纳入 git**（漂移风险 R-NEW-01）。

## 语音入口

| 入口 | 版本/位置 | 状态 |
| --- | --- | --- |
| Windows 客户端 | 本地 `voice-client/`（openWakeWord + VAD） | 生产使用中 |
| XVF3800 卫星固件 | 本地 `voice-client/xvf3800-satellite/`（VAD 模式，WAKE_ENABLED=0） | 上线运行 |
| PWA | VM `services/pwa/www`（index/app.js/sw.js/manifest） | 容器运行中，功能未审查 |
| 手机端 | unverified | 未采集设备与版本 |

## 可写播放状态的路径（全集）

1. voice-gateway `/command`、`/agent`（经 HA script）
2. HA scripts：music_play_query / music_play_uri / music_filler / music_volume_set
3. music-fetcher 完成回调 → HA music_play_uri（IMP-02 待阻断）
4. HA automation：每日 03:30 自动充实（→ fetcher）
5. 物理遥控（Squeezebox）——外部人工控制
6. MA Web UI ——外部人工控制
7. PWA：是否含写路径 unverified
