# 架构

## 组件职责

| 组件 | 端口 | 职责 | 明确不做 |
| --- | --- | --- | --- |
| home-assistant | 8123 | 编排层：所有音乐动作从这里下发 | — |
| music-assistant | 8095 | 曲库 + 跨 provider 搜索 + 播放器管理 | — |
| stt-service | 8100 | SenseVoice 转写（16kHz 单声道 WAV → 文本） | 业务逻辑 |
| voice-gateway | 8200 | `/agent` LLM 会话式意图 · `/command` 规则兜底 · `/tts` 播报合成 | 不直连 MA |
| music-fetcher | 8300 | 按需补库（musicdl）、推荐、口味画像、每日充实 | 不下发播放 |
| 语音客户端 | — | 采音 → 唤醒/断句 → 上传 → 状态显示 | 不做意图判断 |

**架构不变量（D13）**：工具层只产出「意图」，音乐动作一律由 HA script 下发。
这保证了：任何前端（Windows / 嵌入式 / Web PWA）可以随插随换，Agent 侧无需重写。

## 语音链路（一次点播的完整路径）

```
① 唤醒 → 录音（16kHz/mono/s16）→ POST :8100/transcribe → 文本
② POST :8200/agent
     ├─ Fast Path：正则命中「播放X」→ <1ms
     └─ Smart Path：LLM 调工具（含上下文/指代/反馈），秒级
③ HA script.music_play_query
     Step 1  MA music/search（跨 provider，limit 10）
     Step 2  在结果里找 library:// 前缀条目（有 → 播本地）
     Step 3  没有 → 播在线版垫场（QQ 60s 试听）+ 队列自动追加本地填充曲
     Step 4  allow_backfill=true → POST :8300/backfill（异步，返回 job_id）
④ music-fetcher
     查本地库二次确认 → musicdl 搜索 → 守卫筛选 → 下载 → ffprobe 校验
     → 落盘 /music → MA music/sync 重扫 → 回调 HA script.music_play_uri 播完整版
```

### 关键判据

`music/search` 是跨 provider 的全局搜索——库里没有的歌也会返回在线结果。
**「库里有没有」的判据必须是结果里有没有 `library://` 前缀的条目**，而不是
「有没有返回结果」。

## 补库守卫（防下垃圾）

排序键：相关度降序 → 格式偏好 → 歌名长度升序 → 参与歌手数升序 → 有效码率
降序 → 时长降序。

硬拒收：时长 <60s 或 >15min、翻唱/器乐/片段标记、查询词不在歌名歌手里、
歌名含 ≥2 查询词但歌手零匹配、串烧、单文件 >80MB、加密格式（mgg/mflac/kgm）。

繁简归一：聚合音源可能返回繁体（「周杰倫」），统一用 zhconv 折叠后匹配。

**决策层必须把「已筛清单」传给下载层**（`--approved`），否则下载层会自己
再搜再选，绕过全部守卫。

## 口味画像与每日充实

- 信号：曲目播放（`complete` +3 / `skip_fast`(<30s) −2 / `skip_partial`）、
  显式反馈（赞 +10 / 弃 −10 / 拉黑歌手）
- 90 天半衰期；加权随机不放回抽样选种子（防固定 top1 信息茧房）
- 探索池读网易真实榜单（热歌/新歌/飙升），随机榜 + 榜内随机挑「库里没有」
- 配额 = n 中 round(n×0.66) 个口味种子 + 其余探索；冷启动退回「库内最多歌手」
- 补库提交走异步任务（`/enrich/submit` 立即返回 202），HA 侧不阻塞不超时

## 会话与上下文

- `/agent` 是 Session 化的：`session_id` 隔离上下文（不同房间/设备各用各的）
- 会话内维护最近 N 条对话 + 最近播放的曲目，支持「这个」「刚才那首」等指代
- LLM 只产出「意图 + 参数」，音乐动作经 HA script 下发（D13）
- TTS 播报由 gateway 合成（edge-tts 语法），客户端负责回放并在播报期间屏蔽唤醒
