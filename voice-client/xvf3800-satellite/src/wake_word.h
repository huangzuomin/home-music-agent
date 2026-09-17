// =====================================================================
// microWakeWord 唤醒词（v0.2）—— hey_jarvis，与 Windows 客户端同款
//
// 组成：
//   src/msf/    esp-micro-speech-features（梅尔频谱前端，tflite-micro 移植）
//   src/mww/    ESPHome micro_wake_word 的 streaming_model 剥离版
//               （来源 openensemble/voice-device-firmware，Apache-2.0）
//   本文件      前端 + 模型推理的胶水
//
// 音频契约：16kHz 单声道 int16，喂 320 采样（20ms）→ 前端按 30ms 窗/
// 10ms 步进出 40 维特征 → 模型流式推理 → 5 窗滑平均 ≥ 阈值即唤醒
// =====================================================================
#pragma once
#include <cstddef>
#include <cstdint>

namespace wakeword {
// 装载前端与模型；失败返回 false（主循环自动退回 VAD 直接触发模式）
bool init();
// 喂一段音频。返回 true = 检测到唤醒词（每次触发后自动清零概率窗）
bool feed(const int16_t *samples, size_t n);
void enable(bool on);
bool is_enabled();
uint8_t last_average_probability();   // 0-100，日志用
}  // namespace wakeword
