# XVF3800 卫星固件（reSpeaker XVF3800 + XIAO ESP32S3）

把 reSpeaker XVF3800 的 4 麦阵列（XMOS 波束成形/AEC/降噪/DoA）变成
**独立于 PC 的房间级语音入口**：XIAO ESP32S3 经 I2S 拿到 XMOS 处理好的音频，
本地 VAD 断句后直接上传服务端——与 Windows 客户端共用同一套契约，服务端零改动。

> 唤醒词（microWakeWord，hey_jarvis）已集成，代码在 `src/wake_word.cpp`，
> 由 `include/config.h` 的 `WAKE_ENABLED` 开关控制。当前默认关闭（模型
> 灵敏度调优中），固件以 VAD 自动静默检测模式运行。

## 硬件事实（实测，动代码前必读）

1. **GPIO2（D1）是 XMOS 的复位线，低电平 = 按住复位**。悬空或拉低时 XMOS
   整个不上线：I2C 无 0x2C 应答、无 I2S 时钟、LED 灯环亮一下即灭。固件在
   启动时拉高并保持（`xmos_probe()`）。
2. **I2S 主从角色**：官方 ESPHome 配置标注 `secondary`（从机），但实测 XMOS
   不主动出时钟——**ESP32 做主机（MASTER）才有数据**（环境噪声 -60dBFS 级
   别的正确音频）。固件默认 MASTER，异常时自动降级从机重试。
3. 出厂固件即 I2S 应用（带 XIAO 的版本）：XIAO 口接电脑会枚举出 ESP32 串口
   （COMx），XMOS 口在 I2S 固件下插电脑**什么都不会出现**（I2C DFU only），
   不是故障。日常供电/烧录只用 XIAO 口，XMOS 由板间连接器取电。

## 构建与烧录

```powershell
# ① vendor tflite 运行时（一次性；eloquentarduino/tflm_esp32 对流式模型
#    输出恒 0，必须用 espressif 官方 esp-tflite-micro）
tools\vendor-esp-tflite.ps1        # 或 bash tools/vendor-esp-tflite.sh

# ② 配置 include/config.h（WiFi、服务端 IP、触发方式）

# ③ 构建（首次自动下载工具链）
python -m platformio run -d .

# ④ 烧录 + 监视（数据线插 XIAO 口）
python -m platformio run -d . -t upload --upload-port COM7
python -m platformio device monitor
```

PlatformIO 建议装在独立 venv：`python -m venv .pio-venv` +
`.pio-venv\Scripts\pip install platformio`。

## 功能

- I2S 采集 XMOS 处理后音频（16kHz，立体声两路：会议混音 / ASR，`ASR_CHANNEL` 可选）
- VAD 自动断句（能量法 + 自适应噪声底，1200ms 前滚防吞句首）或按钮触发
- 上传 `:8100/transcribe`（multipart）→ `/agent`（JSON）→ 状态灯反馈
- 板载 WS2812 状态灯：绿待命 / 蓝录音 / 橙识别 / 黄 Agent / 红故障
- 开机按住按钮 = 电平表自检（对应客户端 `--mic-level`）
- microWakeWord 唤醒词（`WAKE_ENABLED=1`，默认关）

## 调试基础设施

- **日志 Serial + UDP 双写**：`LOG_HOST`/`LOG_PORT` 指向内网一台机器，
  监听脚本 `python3 -u udp_listener.py`（绑定 :9999）。USB 串口（原生
  CDC）久跑可能哑，UDP 这条一定看得到。
- **模型 io 诊断**：启动时装载模型并打印输入/输出张量的类型/维度/量化
- **合成特征自测**：启动时喂全高/全低特征各 150 片，验证推理通路
- ⚠️ 原生 USB CDC 的 RTS/DTR **不能复位板子**，复位请用 esptool 或断电

## 已知问题

- 唤醒词模型（hey_jarvis v2）对真人语音的响应度偏低（滑窗均值远低于 0.97
  触发线），调优中；TTS 回放可到 ~15%。特征管线与 ESPHome 官方实现逐字节
  一致，怀疑点在前端 fork 与模型训练分布的细微差异。
- 间歇性崩溃（数分钟一次）曾在部分构建出现，怀疑堆损坏残留，未最终定位；
  官方运行时 + 越界修复后出现频率显著下降。

## License 说明

`data/models/hey_jarvis.*` 来自 [esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models)（Apache-2.0，作者 Kevin Ahrendt）；
`src/msf/` 来自 [esphome-libs/esp-micro-speech-features](https://github.com/esphome-libs/esp-micro-speech-features)（Apache-2.0）；
`src/mww/streaming_model.*` 改编自 [ESPHome micro_wake_word 组件](https://github.com/esphome/esphome)（Apache-2.0），
与 [openensemble/voice-device-firmware](https://github.com/openensemble/voice-device-firmware) 的剥离版（Apache-2.0）。
