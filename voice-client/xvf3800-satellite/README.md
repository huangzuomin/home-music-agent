# XVF3800 卫星固件（v0.1）

Home Music Agent 的**独立语音入口**：XVF3800 麦克风阵列 + XIAO ESP32S3，
替代 Windows voice-client 的「耳朵」。服务端零改动。

架构与里程碑见项目根目录《XVF3800-独立语音入口-接入方案.md》。

## v0.1 范围

- I2S 从机采集 XMOS 处理后的音频（16kHz/32bit/立体声，引脚=官方 ESPHome 配置）
- 能量 VAD 自动断句（可切按钮模式），1200ms 前滚防吞句首
- 上传 `:8100/transcribe`（multipart，同 voice_client.py 契约）
- 下发 `:8200/agent`（JSON，session 独立为 `xvf3800-desk`）
- XIAO 板载 RGB 状态灯：绿待命 / 蓝录音 / 橙识别 / 黄 Agent / 红故障

**不在 v0.1**：唤醒词（v0.2 microWakeWord hey_jarvis）、TTS 本地回放（v0.3，
经 I2S DOUT→XMOS→3.5mm，方案 A 自带 AEC 参考）。v0.1 点播是否成功**听音响**验证。

## 构建 & 烧录

```powershell
# 1. 装 PlatformIO（一次性；推荐 venv，别污染系统解释器，理由同 README 解释器陷阱）
python -m venv .pio-venv
.\.pio-venv\Scripts\pip install platformio

# ⚠️ 本机 C 盘空间紧张（2026-09-16 曾 100% 满），PlatformIO 核心已挪到 D:\.platformio。
#    每次构建/烧录都要带 PLATFORMIO_CORE_DIR，否则它默认去 .platformio：
$env:PLATFORMIO_CORE_DIR = "D:\.platformio"
.\.pio-venv\Scripts\pio run            # 构建（首次自动下工具链，较久）

# 2. 数据线插 XIAO 的 USB-C 口（注意：不是 XMOS 那个口！），确认 COM 号
.\.pio-venv\Scripts\pio device list

# 3. 烧录 + 监视
.\.pio-venv\Scripts\pio run -t upload --upload-port COM7
.\.pio-venv\Scripts\pio device monitor
```

> ⚠️ 固件烧录/日志走 **XIAO 口**；XMOS 口（靠 3.5mm 那个）在 I2S 固件下
> 插电脑什么都不会出现（今天已实测），平时不用碰。
> 供电单根线即可：XMOS 由 XIAO 5V 通过板间连接器供电（官方 HA 卫星同款用法）。

## 首次使用检查单

1. `include/config.h`：改 WiFi；确认三个服务端 IP（默认 192.168.1.50）
2. 烧录后开监视器，看到 `WiFi OK` + `就绪`，灯变绿
3. **电平自检**：开机瞬间按住板上按钮 → 电平表模式。静音段噪声底应在
   -60 dBFS 上下，说话段 -40~-20 dBFS（对齐 voice-client 实测参考值）
   - 若恒为 -120：I2S 没数据 → 查 ASR_CHANNEL（0/1 换）、检查 XMOS 是否上电（灯环亮不亮）
4. 正常模式对它说「播放周杰伦的稻香」，监视器依次出现
   `[rec] → [stt] 文本 → [agent] intent`，音响开始播放即端到端通
5. 会话隔离：Agent 日志里 session=xvf3800-desk，与客厅会话互不串上下文

## 已知差异（vs Windows 客户端）

| 项 | Windows 客户端 | 本固件 v0.1 |
| --- | --- | --- |
| 触发 | openWakeWord「hey_jarvis」 | VAD 自动断句 / 按钮（无唤醒词，房间内说话即录） |
| TTS 播报 | 本机播放 | 只打日志（v0.3 实现） |
| 防自唤醒 | pause_wake 闸门 | 不适用（v0.1 不播声音；音乐大声音量下可能误触发） |
| 断句 | 能量 VAD | 能量 VAD（同参数起步） |

## 硬件坑（当天实测，动代码前必读）

1. **GPIO2 = XMOS 复位线，必须拉高并保持**；悬空/拉低 = XMOS 不上线
   （I2C 无 0x2C、无 I2S、灯环闪一下即灭）。
2. **I2S 实际角色：ESP32 = MASTER，XMOS = 从机**。官方 yaml `secondary`
   命名有误导；从机等不到时钟，固件已做成「从机 5s → 自动切 MASTER」。
3. **multipart 组包余量 ≥320 字节**，否则越界写坏 PSRAM 堆，症状是
   WiFi 一分配内存就 LoadProhibited（崩溃点不在肇事代码）。
4. 观测通道：UDP 日志发 VM1:9999（`/tmp/udp.log`，`udp_listener.py` 在
   /tmp）；USB 串口（COM7）在复位后可靠、久跑可能哑，别依赖。

## 里程碑

- **v0.2 唤醒词**：microWakeWord（tflite）模型，官方 ESPHome 配置已验证
  `hey_jarvis` 模型可用；期间用 `TRIGGER_VAD` 过渡
- **v0.3 TTS 回放（方案 A）**：/tts 音频解码 → I2S DOUT(GPIO44) → XMOS →
  3.5mm 小喇叭；XVF3800 的 AEC 以自身输出为参考，播报期间不自唤醒
- **v0.4 灯环**：I2C(GPIO5/6) 控制 XMOS RGB 灯环 + DoA 可视化
  （参考 formatBCE/Respeaker-Lite-ESPHome-integration 的 respeaker_lite 组件）
