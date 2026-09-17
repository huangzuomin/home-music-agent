#include "wake_word.h"

#include <Arduino.h>
#include <cstring>

#include "mww/preprocessor_settings.h"
#include "mww/streaming_model.h"
#include "msf/include/frontend.h"
#include "msf/include/frontend_util.h"

// 主程序提供的日志（Serial + UDP 双写）
extern void sat_log(const char *msg);

// hey_jarvis_model.cpp 生成
extern const unsigned char hey_jarvis_tflite[];
extern const unsigned int hey_jarvis_tflite_len;

namespace wakeword {

static WakeWordModel *model = nullptr;
static FrontendConfig fe_cfg;
static FrontendState fe_state;
static bool fe_ready = false;
static bool enabled_ = false;
static uint8_t last_avg = 0;

// hey_jarvis.json (v2)：cutoff 0.97、滑窗 5、arena 22860
static const uint8_t PROBABILITY_CUTOFF = 247;   // 0.97 * 255
static const size_t SLIDING_WINDOW = 5;
static const size_t TENSOR_ARENA_SIZE = 22860;

// 特征量化：与 ESPHome micro_wake_word generate_features_ 一致
static inline int8_t scale_feature(uint16_t v) {
    constexpr int32_t value_scale = 256;
    constexpr int32_t value_div = 666;   // 25.6 * 26.0
    int32_t value = (((int32_t)v) * value_scale + (value_div / 2)) / value_div;
    value += INT8_MIN;
    if (value < INT8_MIN) value = INT8_MIN;
    if (value > INT8_MAX) value = INT8_MAX;
    return (int8_t)value;
}

static void populate_frontend_config(FrontendConfig &cfg) {
    cfg.window.size_ms = FEATURE_DURATION_MS;                    // 30ms 窗
    cfg.window.step_size_ms = 10;                                // 10ms 步
    cfg.filterbank.num_channels = PREPROCESSOR_FEATURE_SIZE;     // 40
    cfg.filterbank.lower_band_limit = FILTERBANK_LOWER_BAND_LIMIT;
    cfg.filterbank.upper_band_limit = FILTERBANK_UPPER_BAND_LIMIT;
    cfg.noise_reduction.smoothing_bits = NOISE_REDUCTION_SMOOTHING_BITS;
    cfg.noise_reduction.even_smoothing = NOISE_REDUCTION_EVEN_SMOOTHING;
    cfg.noise_reduction.odd_smoothing = NOISE_REDUCTION_ODD_SMOOTHING;
    cfg.noise_reduction.min_signal_remaining = NOISE_REDUCTION_MIN_SIGNAL_REMAINING;
    cfg.pcan_gain_control.enable_pcan = PCAN_GAIN_CONTROL_ENABLE_PCAN;
    cfg.pcan_gain_control.strength = PCAN_GAIN_CONTROL_STRENGTH;
    cfg.pcan_gain_control.offset = PCAN_GAIN_CONTROL_OFFSET;
    cfg.pcan_gain_control.gain_bits = PCAN_GAIN_CONTROL_GAIN_BITS;
    cfg.log_scale.enable_log = LOG_SCALE_ENABLE_LOG;
    cfg.log_scale.scale_shift = LOG_SCALE_SCALE_SHIFT;
}

bool init() {
    populate_frontend_config(fe_cfg);
    memset(&fe_state, 0, sizeof(fe_state));
    if (!FrontendPopulateState(&fe_cfg, &fe_state, 16000)) {
        sat_log("[wake] FrontendPopulateState failed");
        return false;
    }
    fe_ready = true;

    model = new WakeWordModel("hey_jarvis", hey_jarvis_tflite, PROBABILITY_CUTOFF,
                              SLIDING_WINDOW, "Hey Jarvis", TENSOR_ARENA_SIZE, true);
    if (!model) {
        sat_log("[wake] 模型对象分配失败");
        return false;
    }
    enabled_ = true;
    // 立即装载模型，把张量 io 诊断发出去（UDP 可见，不依赖串口）
    if (!model->load_now()) {
        sat_log("[wake] 模型装载失败");
        return false;
    }
    char buf[192];
    snprintf(buf, sizeof(buf), "[wake] %s", streaming_model_io_info());
    sat_log(buf);

    // 合成自测：全高特征 vs 全低特征，验证模型是否真的在计算
    {
        int8_t f[PREPROCESSOR_FEATURE_SIZE];
        int hi = 0, lo = 0;
        memset(f, 120, sizeof(f));
        for (int i = 0; i < 150; i++) {
            model->perform_streaming_inference(f);
            DetectionEvent ev = model->determine_detected();
            if (ev.max_probability > hi) hi = ev.max_probability;
        }
        memset(f, -120, sizeof(f));
        for (int i = 0; i < 150; i++) {
            model->perform_streaming_inference(f);
            DetectionEvent ev = model->determine_detected();
            if (ev.max_probability > lo) lo = ev.max_probability;
        }
        model->reset_probabilities();
        snprintf(buf, sizeof(buf), "[wake] 自测: 高特征输出=%d 低特征输出=%d %s",
                 hi, lo, (hi > 0 || lo > 0) ? "(模型在计算)" : "(输出恒0——tflm/算子问题)");
        sat_log(buf);
    }
    sat_log("[wake] hey_jarvis 就绪（阈值 0.97，滑窗 5）");
    return true;
}

bool feed(const int16_t *samples, size_t n) {
    if (!fe_ready || !model || !enabled_) return false;

    const int16_t *p = samples;
    size_t remaining = n;
    bool detected = false;
    static uint32_t slice_count = 0;
    static int feat_min = 127, feat_max = -128;
    static uint32_t infer_us_accum = 0, infer_count = 0;
    static uint32_t last_feed_us = 0;
    uint32_t t_feed0 = micros();
    while (remaining > 0) {
        size_t processed = 0;
        FrontendOutput out =
            FrontendProcessSamples(&fe_state, p, remaining, &processed);
        if (processed == 0) break;                 // 凑不满一个步长
        p += processed;
        remaining -= processed;
        if (out.size == 0) continue;

        int8_t features[PREPROCESSOR_FEATURE_SIZE] = {0};
        size_t take =
            out.size < PREPROCESSOR_FEATURE_SIZE ? out.size : PREPROCESSOR_FEATURE_SIZE;
        for (size_t i = 0; i < take; i++) {
            features[i] = scale_feature(out.values[i]);
            if (features[i] < feat_min) feat_min = features[i];
            if (features[i] > feat_max) feat_max = features[i];
        }
        uint32_t t0_infer = micros();

        if (!model->perform_streaming_inference(features)) {
            sat_log("[wake] 推理失败");
            return false;
        }
        DetectionEvent ev = model->determine_detected();
        last_avg = ev.average_probability;
        slice_count++;
        infer_us_accum += (micros() - t0_infer);
        infer_count++;
        if (slice_count % 250 == 0) {
            char buf[160];
            snprintf(buf, sizeof(buf),
                     "[wake] slices=%u feat[%d..%d] prob avg=%u max=%u ignore=%d "
                     "infer=%uus/slice feed=%uus",
                     (unsigned)slice_count, feat_min, feat_max,
                     ev.average_probability, ev.max_probability,
                     (int)model->get_ignore_windows(),
                     (unsigned)(infer_count ? infer_us_accum / infer_count : 0),
                     (unsigned)last_feed_us);
            sat_log(buf);
            infer_us_accum = 0;
            infer_count = 0;
            feat_min = 127;
            feat_max = -128;
        }
        if (ev.detected) {
            model->reset_probabilities();          // 触发后重新武装
            detected = true;
        }
    }
    return detected;
}

void enable(bool on) {
    enabled_ = on;
    if (model) on ? model->enable() : model->disable();
}
bool is_enabled() { return enabled_; }
uint8_t last_average_probability() { return last_avg; }

}  // namespace wakeword
