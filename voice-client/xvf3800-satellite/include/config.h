// =====================================================================
// XVF3800 卫星固件配置 —— 对应 voice-client/config.json 的角色
// 服务端契约见《XVF3800-独立语音入口-接入方案.md》，与 voice_client.py 一致
// =====================================================================
#pragma once

// ---- WiFi ----
#define WIFI_SSID      "YourWiFiSSID"
#define WIFI_PASSWORD  "YourWiFiPassword"

// ---- 服务端地址（与 voice-client/config.json 相同的三件套） ----
#define STT_URL    "http://192.168.1.50:8100/transcribe"
#define AGENT_URL  "http://192.168.1.50:8200/agent"
#define TTS_URL    "http://192.168.1.50:8200/tts"

// 独立会话 id：不要和 living-room-current 混用，上下文互不干扰
#define SESSION_ID "xvf3800-desk"

// ---- 唤醒词（v0.2）----
// 1 = 需要喊 Hey Jarvis 才响应
// 0 = VAD 自动断句（说话即响应）
//
// ⚠️ IMP-02（T16/G05）：WAKE_ENABLED=0 属于**调试模式**——房间里任何说话
//    都会被采集上传，无唤醒保护。仅限开发调试时显式选择；家庭日常使用
//    应置 1（或用按钮触发）。运行时日志与状态灯会以「琥珀色」明示。
#define WAKE_ENABLED   0

// ---- 触发方式 ----
// 1 = VAD 自动断句（免按键，v0.1 默认；v0.2 换 ESP-SR/microWakeWord 唤醒）
// 0 = 按住 XVF3800 板上按钮说话（GPIO3，低电平有效），松开识别
#define TRIGGER_VAD  1

// ---- I2S（XIAO 为从机，XMOS 出时钟；引脚来自官方 ESPHome 配置） ----
#define I2S_BCLK_PIN   8     // D9
#define I2S_WS_PIN     7     // D8 (LRCLK)
#define I2S_DIN_PIN    43    // D6（XMOS -> XIAO 麦克风数据）
#define I2S_DOUT_PIN   44    // D7（XIAO -> XMOS 扬声器数据，v0.2 TTS 用）
#define BUTTON_PIN     3     // D2，板上按钮，按下为低
#define XMOS_RESET_PIN 2     // D1，XMOS 复位线（respeaker_lite 组件同款用法）

// 出厂 I2S 固件输出立体声：ch0=会议混音，ch1=ASR。唤醒词用 ch1 实测中。
// 0 = 左声道(ch0)，1 = 右声道(ch1)。识别不对就换这边。
#define ASR_CHANNEL    1

// ---- 采样与缓冲（对齐 voice-client config.json 的 voice 段） ----
#define SAMPLE_RATE      16000
#define FRAME_MS         20                     // 一帧 20ms = 320 采样
#define RING_PREROLL_MS  1200                   // 防吞句首
#define MAX_UTTERANCE_S  15                     // 与 max_utterance_sec 一致
#define END_SILENCE_MS   800                    // 与 end_silence_ms 一致

// ---- VAD 阈值（dBFS，能量法；实测后可调） ----
#define VAD_START_DB     -38.0f                 // 说话起点（超过噪声底 + 此值也算）
#define VAD_START_ABOVE_NOISE_DB  10.0f
#define VAD_END_DB       -46.0f                 // 静音判定
#define NOISE_TRACK_ALPHA  0.05f                // 噪声底慢速跟随

// ---- UDP 日志（USB 串口不可靠时的观测通道） ----
#define LOG_HOST "192.168.1.50"   // VM1（Ubuntu 侧监听，无防火墙问题）
#define LOG_PORT 9999

// ---- 状态灯（XIAO 板载 WS2812，GPIO1） ----
#define LED_PIN   1
#define LED_COUNT 1
