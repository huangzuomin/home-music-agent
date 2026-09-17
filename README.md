# Home Music Agent · 家庭智能音乐系统

> 说一句话，播放点歌：曲库里没有的歌，自动在线垫场 + 后台下载补齐。

**English**: A self-hosted, voice-driven home music assistant. Speak a natural-language
request ("play something suitable for working"); a local LLM agent resolves the intent,
searches your library and online providers, plays an online preview immediately while a
background fetcher downloads the full track into your library, then seamlessly switches
to it. Runs entirely on your home network — Home Assistant + Music Assistant + a local
STT service + a voice gateway.

---

## 功能一览

- **语音点歌**：麦克风说话 → 本地 SenseVoice 转写 → LLM Agent 解析意图 → Home Assistant 编排 → Music Assistant 播放
- **按需补库**：点播库外歌时，先播在线试听垫场，后台用 musicdl 聚合音源下载完整版，自动入库并替换播放
- **会话式交互**：支持上下文与指代（「这个」「刚才那首」「后面放点安静的」），支持音量控制、播放暂停、场景需求（「来点适合工作的音乐」）
- **口味画像与每日充实**：基于播放/跳过/显式反馈的加权画像（90 天半衰期）+ 网易榜单探索，每天自动推荐并补库
- **多端播放**：Squeezebox、Google Home / Cast 音箱等（由 Music Assistant 管理播放器）
- **嵌入式语音入口（v0.2）**：reSpeaker XVF3800 + XIAO ESP32S3 固件，独立于 PC 的房间级拾音前端

## 架构

```
语音入口（Windows 客户端 / reSpeaker XVF3800 卫星固件）
      │ 唤醒词 + 录音
      ▼
Ubuntu VM（Docker Compose，/opt/home-music-agent）
 ├─ home-assistant    :8123   编排层：所有音乐动作从这里下发
 ├─ music-assistant   :8095   曲库 + 跨 provider 搜索 + 播放器管理
 ├─ stt-service       :8100   SenseVoice 语音转文字
 ├─ voice-gateway     :8200   /agent（LLM 会话式意图解析）· /command（规则兜底）· /tts
 └─ music-fetcher     :8300   按需补库（musicdl 聚合音源）
      │
      ├─ NAS 音乐库（NFS 挂载为 /music）
      └─ 播放器：Squeezebox Touch · Google Home / Cast 音箱
```

一条点播指令的完整路径：

```
① 唤醒 → 录音 → POST :8100/transcribe → 文本
② POST :8200/agent
     ├─ Fast Path：正则命中「播放X」→ <1ms
     └─ Smart Path：LLM 调工具（上下文/指代/反馈），秒级
③ HA script.music_play_query
     → MA music/search（跨 provider）→ 有本地曲目：播本地
④ 库里没有 → 播在线版垫场（填充曲自动补位）
   → POST :8300/backfill（异步）→ 守卫筛选 → 下载 → MA 重扫 → 回调播完整版
```

**架构不变量（D13）**：工具层只产出「意图」，音乐动作一律由 Home Assistant 统一下发；
music-fetcher 只对 MA 做「查库」和「触发重扫」两件事。任何服务不直连 MA 播放。

## 目录结构

```
├── compose.yaml                  # VM 上的一键编排
├── .env.example                  # 凭据模板（复制为 .env 填写）
├── services/
│   ├── stt/                      # SenseVoice 转写服务
│   ├── voice-gateway/            # LLM 会话式意图网关
│   ├── music-fetcher/            # musicdl 按需补库 + 推荐 + 口味画像
│   ├── home-assistant/packages/  # HA 编排包（script.* / automation）
│   └── music-assistant/tools/    # 播放历史工具
├── voice-client/                 # Windows 薄客户端（openWakeWord + VAD）
│   └── xvf3800-satellite/        # reSpeaker XVF3800 + XIAO ESP32S3 卫星固件
├── tools/                        # 固件依赖 vendor 脚本
└── docs/                         # 架构 / 部署 / 踩坑宝典
```

## 快速开始

### 1. 服务端（Ubuntu VM / 常驻主机）

前置：Docker Compose、Home Assistant 与 Music Assistant 的管理员账号、一个 NAS
音乐目录（或本机目录）、一个 LLM API Key（OpenAI 兼容协议均可，默认 DeepSeek）。

```bash
git clone https://github.com/huangzuomin/home-music-agent.git
cd home-music-agent
cp .env.example .env       # 填写 HA/MA 账号、LLM Key、服务器 IP
docker compose up -d --build
```

然后把 `services/home-assistant/packages/home_music_agent.yaml` 复制到你的 HA
`config/packages/` 并重载脚本，详见 [docs/deploy.md](docs/deploy.md)。

### 2. 语音客户端（Windows）

详见 [docs/voice-client.md](docs/voice-client.md)。要点：Python 3.10+、
`pip install openwakeword requests`、复制 `config.example.json` 为 `config.json`
并填入服务端 IP，运行 `run.bat --voice`。

### 3. 嵌入式语音入口（可选，reSpeaker XVF3800 + XIAO ESP32S3）

详见 [docs/firmware.md](docs/firmware.md)。固件把 XVF3800 麦克风阵列变成
**独立于 PC 的房间级拾音入口**，与 Windows 客户端共用同一套服务端契约。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 组件职责、语音链路、补库链路、口味画像、架构不变量 |
| [docs/deploy.md](docs/deploy.md) | 服务端部署、HA 包安装、凭据铸造、运维速查 |
| [docs/voice-client.md](docs/voice-client.md) | Windows 客户端：安装、配置、自检、实现约束 |
| [docs/firmware.md](docs/firmware.md) | XVF3800 固件：构建烧录、硬件坑、调试基础设施 |
| [docs/pitfalls.md](docs/pitfalls.md) | 踩坑宝典：真实运行中踩出来并修复的问题全集 |

## 已知限制

- **musicdl 许可证为 PolyForm Noncommercial**：music-fetcher 的补库功能仅限
  个人/非商业用途；请在你所在司法辖区遵守版权法律，本项目的下载功能仅供
  个人学习研究。
- 曲库按需补库依赖第三方聚合音源，接口随时可能失效（守卫规则是质量屏障）。
- 未登录音乐平台账号时，在线垫场只有约 60 秒试听。
- 嵌入式入口的唤醒词（hey_jarvis）仍在调优中，当前固件默认为 VAD 直接触发模式。

## 硬件参考（非必需）

| 硬件 | 用途 |
| --- | --- |
| reSpeaker XVF3800（XIAO ESP32S3 版） | 4 麦阵列 + XMOS 回声消除/波束成形，房间级语音入口 |
| Squeezebox Touch / 任意 Cast 音箱 | Music Assistant 管理的播放端 |
| 群晖/TrueNAS 等 NAS | 曲库存储（NFS/SMB 挂载） |

## License

[MIT](LICENSE)。第三方组件遵循其各自许可证：microWakeWord 模型与
esp-micro-speech-features（Apache-2.0）、esp-tflite-micro（Apache-2.0）、
musicdl（PolyForm Noncommercial，非商业用途）。
