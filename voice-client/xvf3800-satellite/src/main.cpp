// =====================================================================
// Home Music Agent — XVF3800 独立语音入口（v0.1）
//
// 角色：替代 voice-client（Windows 薄客户端）的「耳朵」。
//   XMOS 阵列(I2S) -> 本固件(触发/断句) -> STT :8100 -> Agent :8200
// 服务端零改动；契约与 voice_client.py 完全一致。
//
// v0.1 范围：VAD 自动断句 / 按钮触发 -> 上传识别 -> 下发 Agent。
//   唤醒词（microWakeWord hey_jarvis）与 TTS 本地回放是 v0.2/v0.3，
//   见《XVF3800-独立语音入口-接入方案.md》里程碑。
//
// 引脚来源：官方 respeaker/reSpeaker_XVF3800_ESPHome_Assistant@master
// =====================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include <Wire.h>
#include <WiFiUdp.h>
#include <driver/i2s.h>

#include "config.h"
#include "wake_word.h"

// ---------------------------------------------------------------- 状态

enum SatState : uint8_t { ST_IDLE, ST_CAPTURE, ST_STT, ST_AGENT };
static SatState g_state = ST_IDLE;
static bool g_wifi_ok = false;
static bool wake_ok = false;          // 唤醒词引擎是否可用
static bool g_muted = false;          // IMP-12：真实闭麦（输入端停止采集）

static Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
static WiFiUDP g_udp;

static void ledSet(uint8_t r, uint8_t g, uint8_t b) {
    strip.setPixelColor(0, strip.Color(r, g, b));
    strip.show();
}

static void ledState(SatState st) {
    // IMP-12（T15）：闭麦是最高优先级状态——红色实心，输入端停止采集。
    if (g_muted) {
        ledSet(120, 0, 0);
        return;
    }
    // IMP-02（T16）：无唤醒的 VAD 常驻是调试模式——空闲灯用琥珀色明示，
    // 与正式的绿色（唤醒模式待命）区分，避免家人误以为已受唤醒保护。
    if (st == ST_IDLE && !WAKE_ENABLED) {
        ledSet(255, 120, 0);  // 琥珀：调试模式待命
        return;
    }
    switch (st) {
        case ST_IDLE:     ledSet(0, 40, 0);   break;  // 绿：待命
        case ST_CAPTURE:  ledSet(0, 0, 255);  break;  // 蓝：录音
        case ST_STT:      ledSet(255, 140, 0);break;  // 橙：识别
        case ST_AGENT:    ledSet(255, 255, 0);break;  // 黄：Agent
    }
}

static void logf(const char *fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    Serial.println(buf);
    // UDP 双写：串口被 USB-CDC 吞日志时，网络这条一定看得到
    if (g_wifi_ok) {
        g_udp.beginPacket(LOG_HOST, LOG_PORT);
        g_udp.write((const uint8_t *)buf, strlen(buf));
        g_udp.write((const uint8_t *)"\n", 1);
        g_udp.endPacket();
    }
}

// wake_word.cpp 等模块的日志入口
void sat_log(const char *msg) { logf("%s", msg); }

// ---------------------------------------------------------------- 音频缓冲
// 32bit 立体声 DMA 读入 -> 取单声道 s16。utterance + 前滚缓冲都放 PSRAM。

static int16_t *g_utter = nullptr;   // 完整语句（含前滚）
static size_t   g_utter_cap = 0;     // 采样数上限
static size_t   g_utter_len = 0;     // 已写入采样数
static int16_t *g_ring = nullptr;    // 前滚环形缓冲（RING_PREROLL_MS）
static size_t   g_ring_cap = 0;
static size_t   g_ring_pos = 0;
static bool     g_ring_full = false;

static bool audio_buf_init() {
    g_utter_cap = (size_t)SAMPLE_RATE * MAX_UTTERANCE_S;
    g_utter = (int16_t *)ps_malloc(g_utter_cap * sizeof(int16_t));
    g_ring_cap = (size_t)SAMPLE_RATE * RING_PREROLL_MS / 1000;
    g_ring = (int16_t *)ps_malloc(g_ring_cap * sizeof(int16_t));
    if (!g_utter || !g_ring) {
        logf("[FAIL] PSRAM 分配失败（utter=%d ring=%d）psram=%d",
             (int)g_utter_cap, (int)g_ring_cap, (int)psramFound());
        return false;
    }
    return true;
}

// ---------------------------------------------------------------- I2S 采集

static bool g_i2s_master = false;

// master=false：XIAO 从机，等 XMOS 出时钟（官方 ESPHome 配置的角色）
// master=true ：XIAO 自己出时钟（XMOS 若是从机则跟随）——诊断/兜底用
static bool i2s_init(bool master) {
    g_i2s_master = master;
    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_RX | (master ? I2S_MODE_MASTER : I2S_MODE_SLAVE));
    cfg.sample_rate = SAMPLE_RATE;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
    cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;         // 立体声交错
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;    // 不设这项校验直接失败
    cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count = 8;
    cfg.dma_buf_len = 256;                                   // 帧
    cfg.use_apll = false;
    cfg.tx_desc_auto_clear = false;
    cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
    cfg.fixed_mclk = 0;
    if (i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr) != ESP_OK) {
        logf("[FAIL] i2s_driver_install");
        return false;
    }
    i2s_pin_config_t pins = {};
    pins.bck_io_num   = I2S_BCLK_PIN;
    pins.ws_io_num    = I2S_WS_PIN;
    pins.data_out_num = I2S_PIN_NO_CHANGE;                   // v0.2 TTS 启用 44
    pins.data_in_num  = I2S_DIN_PIN;
    if (i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK) {
        logf("[FAIL] i2s_set_pin");
        return false;
    }
    i2s_zero_dma_buffer(I2S_NUM_0);
    return true;
}

// 读一帧（FRAME_MS），转成单声道 s16 写进 out。
// 返回采样数；0=超时无数据（XMOS 没给时钟），-1=读取出错。
static size_t i2s_read_frame_mono(int16_t *out) {
    static int32_t raw[FRAME_MS * SAMPLE_RATE / 1000 * 2];   // 立体声 32bit
    const size_t samples = FRAME_MS * SAMPLE_RATE / 1000;
    size_t br = 0;
    if (i2s_read(I2S_NUM_0, raw, sizeof(raw), &br, pdMS_TO_TICKS(500)) != ESP_OK) {
        return (size_t)-1;
    }
    if (br == 0) return 0;                                   // 500ms 无时钟
    size_t frames = br / (sizeof(int32_t) * 2);
    for (size_t i = 0; i < frames; i++) {
        // 槽序：内存里先 slot0 后 slot1；ASR_CHANNEL 选 0/1
        int32_t v = (ASR_CHANNEL == 0) ? raw[i * 2] : raw[i * 2 + 1];
        v >>= 16;                                            // 32 -> 16
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        out[i] = (int16_t)v;
    }
    return frames;
}

// ---------------------------------------------------------------- 能量 VAD
// 与 voice_engine 的思路一致：噪声底慢速跟随，说话起点取
// max(绝对阈值, 噪声底 + 相对阈值)，静音 END_SILENCE_MS 断句。

static float g_noise_db = -70.0f;

static float frame_dbfs(const int16_t *s, size_t n) {
    if (n == 0) return -120.0f;
    double acc = 0;
    for (size_t i = 0; i < n; i++) acc += (double)s[i] * s[i];
    double rms = sqrt(acc / n) / 32768.0;
    if (rms <= 1e-9) return -120.0f;
    return (float)(20.0 * log10(rms));
}

static bool vad_is_speech(float db) {
    float th = VAD_START_DB;
    if (g_noise_db + VAD_START_ABOVE_NOISE_DB > th) {
        th = g_noise_db + VAD_START_ABOVE_NOISE_DB;
    }
    return db >= th;
}

static void vad_update_noise(float db) {
    // 只在非语音帧跟随，防止把说话当底噪
    if (vad_is_speech(db)) return;
    float lin_db = db;
    g_noise_db += NOISE_TRACK_ALPHA * (lin_db - g_noise_db);
}

// ---------------------------------------------------------------- WAV

static size_t wav_header(uint8_t *h, uint32_t pcm_bytes) {
    uint32_t sr = SAMPLE_RATE, ch = 1, bits = 16;
    uint32_t byte_rate = sr * ch * bits / 8, block = ch * bits / 8;
    memcpy(h + 0, "RIFF", 4);
    *(uint32_t *)(h + 4) = 36 + pcm_bytes;
    memcpy(h + 8, "WAVE", 4);
    memcpy(h + 12, "fmt ", 4);
    *(uint32_t *)(h + 16) = 16;
    *(uint16_t *)(h + 20) = 1;                 // PCM
    *(uint16_t *)(h + 22) = (uint16_t)ch;
    *(uint32_t *)(h + 24) = sr;
    *(uint32_t *)(h + 28) = byte_rate;
    *(uint16_t *)(h + 32) = (uint16_t)block;
    *(uint16_t *)(h + 34) = (uint16_t)bits;
    memcpy(h + 36, "data", 4);
    *(uint32_t *)(h + 40) = pcm_bytes;
    return 44;
}

// ---------------------------------------------------------------- HTTP
// 契约 = voice_client.py：transcribe 用 multipart(file 字段)，agent/tts 用 JSON。

#define BOUNDARY "----HMASatellite7f3a9c2b"

static String http_post_multipart(const char *url, const uint8_t *body, size_t len) {
    HTTPClient http;
    http.begin(url);
    http.setConnectTimeout(4000);
    http.setTimeout(90000);                    // SenseVoice 首次可能加载模型
    http.addHeader("Content-Type", "multipart/form-data; boundary=" BOUNDARY);
    int code = http.POST((uint8_t *)body, len);
    String resp = (code > 0) ? http.getString() : String("");
    if (code != 200) {
        logf("[FAIL] STT HTTP %d %s", code, http.errorToString(code).c_str());
    }
    http.end();
    return (code == 200) ? resp : String("");
}

static String http_post_json(const char *url, const String &json, int timeout_ms,
                             int *out_code) {
    HTTPClient http;
    http.begin(url);
    http.setConnectTimeout(4000);
    http.setTimeout(timeout_ms);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(json);
    String resp = (code > 0) ? http.getString() : String("");
    http.end();
    if (out_code) *out_code = code;
    return (code == 200) ? resp : String("");
}

// ---------------------------------------------------------------- XMOS 探测
// 协议与 respeaker_lite 组件一致：DFU servicer(240)，GETVERSION(88|0x80)。
// 0x18 = TLV320AIC3104 编解码器（寄存器全 0），不是 XMOS；XMOS 应答在 0x2C。

static void xmos_probe(const char *phase) {
    Wire.begin();
    Wire.setClock(400000);
    uint8_t found[8];
    int nfound = 0;
    for (uint8_t a = 8; a < 120 && nfound < 8; a++) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) found[nfound++] = a;
    }
    if (!nfound) {
        logf("[i2c][%s] 无任何应答设备", phase);
        return;
    }
    for (int i = 0; i < nfound; i++) {
        logf("[i2c][%s] 0x%02X 应答", phase, found[i]);
        if (found[i] == 0x18) continue;      // TLV320 编解码器，寄存器全 0，跳过
        // GETVERSION：写 {240, 88|0x80, 4}，读 {status, major, minor, patch}
        uint8_t req[3] = {240, 88 | 0x80, 4};
        Wire.beginTransmission(found[i]);
        Wire.write(req, 3);
        if (Wire.endTransmission() != 0) continue;
        delay(5);
        if (Wire.requestFrom((int)found[i], 4) != 4) {
            logf("[i2c][%s] 0x%02X 版本读取失败", phase, found[i]);
            continue;
        }
        uint8_t r[4];
        for (auto &b : r) b = Wire.read();
        logf("[i2c][%s] 0x%02X 版本 %u.%u.%u (status=%u)", phase, found[i],
             r[1], r[2], r[3], r[0]);

        // 麦克风静音状态：configuration servicer(241) 读 0x81
        uint8_t mq[3] = {241, 0x81, 1};
        Wire.beginTransmission(found[i]);
        Wire.write(mq, 3);
        Wire.endTransmission();
        delay(5);
        if (Wire.requestFrom((int)found[i], 2) == 2) {
            uint8_t m[2];
            for (auto &b : m) b = Wire.read();
            logf("[i2c][%s] 0x%02X 麦克风静音=%u (status=%u)", phase,
                 found[i], m[1], m[0]);
            if (m[1] == 1) {
                // 出厂/意外静音态：写 0x01=0 解除
                uint8_t um[4] = {241, 0x01, 1, 0};
                Wire.beginTransmission(found[i]);
                Wire.write(um, 4);
                Wire.endTransmission();
                logf("[i2c][%s] 已发送解除麦克风静音", phase);
            }
        }

        // GETSTATE(5|0x80)：读 DFU 状态机。非 0 = 卡在引导态（无音频）
        uint8_t gs[3] = {240, 5 | 0x80, 2};
        Wire.beginTransmission(found[i]);
        Wire.write(gs, 3);
        Wire.endTransmission();
        delay(5);
        if (Wire.requestFrom((int)found[i], 4) == 4) {
            uint8_t s[4];
            for (auto &b : s) b = Wire.read();
            logf("[i2c][%s] 0x%02X DFU 状态 %u (bytes %u %u %u %u)", phase,
                 found[i], s[1], s[0], s[1], s[2], s[3]);
            if (s[1] != 0) {
                // CLRSTATUS(4) → ABORT(6) → REBOOT(89)，踢回应用态
                const uint8_t seq[3][4] = {{240, 4, 1, 0}, {240, 6, 1, 0},
                                           {240, 89, 1, 0}};
                for (auto &cmd : seq) {
                    Wire.beginTransmission(found[i]);
                    Wire.write(cmd, 4);
                    Wire.endTransmission();
                    delay(20);
                }
                logf("[i2c][%s] 已发 CLRSTATUS+ABORT+REBOOT", phase);
            }
        }
    }
}

// ---------------------------------------------------------------- 主流程

static void handle_utterance() {
    if (g_utter_len < SAMPLE_RATE / 2) {       // <0.5s 当误触发
        logf("[skip] 过短（%d 采样）", (int)g_utter_len);
        return;
    }
    uint32_t pcm_bytes = (uint32_t)g_utter_len * 2;
    // 余量必须覆盖 multipart 头(~120)+WAV头(44)+尾(~32)+null，曾因只给 128
    // 越界 24 字节写坏 PSRAM 堆元数据 → WiFi 一分配内存就 LoadProhibited
    size_t total = 44 + pcm_bytes + 320;
    uint8_t *body = (uint8_t *)ps_malloc(total);
    if (!body) {
        logf("[FAIL] multipart 缓冲分配失败");
        return;
    }
    size_t off = 0;
    off += snprintf((char *)body + off, 160,
        "--" BOUNDARY "\r\n"
        "Content-Disposition: form-data; name=\"file\"; filename=\"ptt.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n");
    off += wav_header(body + off, pcm_bytes);
    memcpy(body + off, g_utter, pcm_bytes);
    off += pcm_bytes;
    off += snprintf((char *)body + off, 64, "\r\n--" BOUNDARY "--\r\n");

    g_state = ST_STT;
    ledState(g_state);
    uint32_t t0 = millis();
    String stt = http_post_multipart(STT_URL, body, off);
    free(body);                                // free() 可释放 PSRAM 指针
    String text = "";
    if (stt.length()) {
        JsonDocument doc;
        if (deserializeJson(doc, stt) == DeserializationError::Ok) {
            text = doc["text"] | "";
        }
    }
    logf("[stt] %.2fs 文本: %s", (millis() - t0) / 1000.0f,
         text.length() ? text.c_str() : "(空)");
    if (!text.length()) return;

    g_state = ST_AGENT;
    ledState(g_state);
    t0 = millis();
    String req;
    JsonDocument reqdoc;
    reqdoc["text"] = text;
    reqdoc["session_id"] = SESSION_ID;
    reqdoc["user_id"] = "default";
    reqdoc["source"] = "voice";
    reqdoc["want_say"] = true;
    serializeJson(reqdoc, req);
    int code = 0;
    String res = http_post_json(AGENT_URL, req, 25000, &code);
    if (!res.length()) {
        logf("[FAIL] Agent HTTP %d", code);
        ledSet(255, 0, 0);
        delay(800);
        return;
    }
    JsonDocument rdoc;
    String reply = "", intent = "", path = "";
    if (deserializeJson(rdoc, res) == DeserializationError::Ok) {
        reply = rdoc["reply"] | "";
        intent = rdoc["intent"] | "";
        path = rdoc["path"] | "";
    }
    logf("[agent] %.2fs [%s/%s] %s", (millis() - t0) / 1000.0f,
         path.c_str(), intent.c_str(),
         reply.length() ? reply.c_str() : "(无播报)");
    // v0.2：这里接 /tts 拉播报音频并经 I2S DOUT(GPIO44) 回放 XMOS（方案 A，
    // AEC 自带参考）。v0.1 只打日志——点播是否成功听音响即可验证。
}

// 电平表模式：开机按住按钮进入，跑 60s。对应 --mic-level 的验收用途。
static void level_meter_mode() {
    logf("=== 电平表 60s（噪声底 %.1f dBFS）===", g_noise_db);
    logf("先静 3s，再正常说话 3s。说话段应在 -40 ~ -20 dBFS。");
    int16_t mono[FRAME_MS * SAMPLE_RATE / 1000];
    uint32_t t0 = millis();
    float peak = -120;
    while (millis() - t0 < 60000) {
        size_t n = i2s_read_frame_mono(mono);
        if (!n) break;
        float db = frame_dbfs(mono, n);
        if (db > peak) peak = db;
        vad_update_noise(db);
        static uint32_t last = 0;
        if (millis() - last > 500) {
            last = millis();
            logf("rms %.1f dBFS  peak %.1f dBFS  noise %.1f dBFS%s",
                 db, peak, g_noise_db, vad_is_speech(db) ? "  [语音]" : "");
            peak = -120;
        }
    }
    logf("=== 电平表结束 ===");
}

void setup() {
    Serial.begin(115200);
    delay(200);
    strip.begin();
    strip.setBrightness(60);
    ledSet(30, 0, 0);                          // 红：启动中

    pinMode(BUTTON_PIN, INPUT_PULLUP);

    logf("== XVF3800 卫星 v0.1 ==");
    if (!audio_buf_init()) {
        ledSet(255, 0, 0);
        while (true) delay(1000);
    }
    if (!i2s_init(true)) {   // 实测 XMOS 是从机：ESP32 做主机才有数据
        ledSet(255, 0, 0);
        while (true) delay(1000);
    }

    WiFi.mode(WIFI_STA);
    // 启动扫描：确认周围有哪些 2.4G SSID（ESP32 只支持 2.4GHz）
    int found = WiFi.scanNetworks();
    logf("== 扫描到 %d 个网络 ==", found);
    for (int i = 0; i < found; i++) {
        logf("  [%d] %s  ch%d  %ddBm", i, WiFi.SSID(i).c_str(),
             WiFi.channel(i), WiFi.RSSI(i));
    }
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    logf("WiFi 连接 %s ...", WIFI_SSID);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
        delay(250);
    }
    g_wifi_ok = (WiFi.status() == WL_CONNECTED);
    if (!g_wifi_ok) {
        logf("[FAIL] WiFi 连不上，持续重试中");
    } else {
        g_udp.begin(0);
        logf("WiFi OK  IP=%s  RSSI=%d", WiFi.localIP().toString().c_str(),
             WiFi.RSSI());
    }

    // 开机按住按钮 = 电平表自检
    if (digitalRead(BUTTON_PIN) == LOW) {
        level_meter_mode();
    }

    // XMOS：复位线拉高释放 + 控制面探测
    pinMode(XMOS_RESET_PIN, OUTPUT);
    digitalWrite(XMOS_RESET_PIN, HIGH);
    delay(1000);
    logf("[xmos] GPIO2 HIGH 1s");
    xmos_probe("A");
    digitalWrite(XMOS_RESET_PIN, HIGH);       // 保持释放，常 HIGH

    // 唤醒词引擎（v0.2）：WAKE_ENABLED=0 时退回 VAD 直接触发
    wake_ok = WAKE_ENABLED && wakeword::init();
    if (!WAKE_ENABLED) logf("[wake] 已按配置关闭，使用 VAD 直接触发");
    else if (!wake_ok) logf("[wake] 不可用，退回 VAD 直接触发模式");

    if (!WAKE_ENABLED)
        logf("[mode] ⚠️ 调试模式：无唤醒 VAD 常驻（房间内说话即采集上传）");
    logf("就绪。模式：%s", TRIGGER_VAD ? "VAD 自动断句（按钮可强制）" : "按钮按住说话");
    g_state = ST_IDLE;
    ledState(g_state);
}

void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        g_wifi_ok = false;
        ledSet(120, 0, 0);
        WiFi.reconnect();
        delay(1000);
        return;
    }
    if (!g_wifi_ok) {
        g_wifi_ok = true;
        g_udp.begin(0);
        logf("WiFi 恢复 IP=%s", WiFi.localIP().toString().c_str());
        ledState(g_state);
    }

    static int16_t mono[FRAME_MS * SAMPLE_RATE / 1000];
    size_t n = i2s_read_frame_mono(mono);
    if (n == (size_t)-1) {
        logf("[i2s] 读取出错");
        delay(500);
        return;
    }
    if (n == 0) {
        // 无时钟/数据：每 3s 报一次；MASTER 持续 10s 无数据则降级试从机
        static uint32_t starve_start = 0, hb2 = 0;
        if (starve_start == 0) starve_start = millis();
        if (millis() - hb2 > 3000) {
            hb2 = millis();
            logf("[i2s][%s] 无时钟/数据 已 %.1fs",
                 g_i2s_master ? "MASTER" : "slave",
                 (millis() - starve_start) / 1000.0f);
        }
        if (g_i2s_master && millis() - starve_start > 10000) {
            logf("[i2s] MASTER 10s 无数据 -> 降级从机重试");
            i2s_driver_uninstall(I2S_NUM_0);
            i2s_init(false);
            starve_start = 0;
        }
        return;
    }
    float db = frame_dbfs(mono, n);

    static bool capture = false;
    static uint32_t silence_ms = 0, capture_ms = 0;

    bool btn = digitalRead(BUTTON_PIN) == LOW;

    if (!capture && g_state == ST_IDLE) {
        bool trigger = btn;
        if (wake_ok) {
            // 唤醒词模式：只有本地检测到 Hey Jarvis（或按按钮）才开始录音
            if (wakeword::feed(mono, n)) {
                trigger = true;
                logf("[wake] 检测到 Hey Jarvis（滑窗均值 %u%%）",
                     wakeword::last_average_probability());
            }
        } else if (TRIGGER_VAD && vad_is_speech(db)) {
            trigger = true;                    // 兜底：VAD 直接触发
        }
        if (trigger) {
            capture = true;
            silence_ms = capture_ms = 0;
            g_utter_len = 0;
            g_ring_pos = 0;
            g_ring_full = false;
            // 前滚：把环形缓冲倒进语句头（防吞句首，同 ring_buffer_ms）
            if (g_ring_full) {
                memcpy(g_utter, g_ring + g_ring_pos,
                       (g_ring_cap - g_ring_pos) * sizeof(int16_t));
                memcpy(g_utter + (g_ring_cap - g_ring_pos), g_ring,
                       g_ring_pos * sizeof(int16_t));
                g_utter_len = g_ring_cap;
            }
            memcpy(g_utter + g_utter_len, mono, n * sizeof(int16_t));
            g_utter_len += n;
            g_state = ST_CAPTURE;
            ledState(g_state);
            logf("[rec] 开始（触发=%s 噪声底 %.1f）", btn ? "按钮" : "VAD",
                 g_noise_db);
        } else {
            vad_update_noise(db);
            // 心跳：空闲时每 3s 报告电平，用于确认 I2S 有数据、VAD 状态正常
            static uint32_t hb = 0;
            if (millis() - hb > 3000) {
                hb = millis();
                logf("[idle][%s] rms %.1f  noise %.1f  wake %u%%%s",
                     wake_ok ? "唤醒" : "VAD", db, g_noise_db,
                     wakeword::last_average_probability(),
                     (db <= -119.0f) ? "  [!] 全零——查 ASR_CHANNEL/I2S 接线" : "");
            }
            // 环形缓冲整帧写入
            for (size_t i = 0; i < n; i++) {
                g_ring[g_ring_pos] = mono[i];
                g_ring_pos = (g_ring_pos + 1) % g_ring_cap;
                if (g_ring_pos == 0) g_ring_full = true;
            }
        }
        return;
    }

    if (capture) {
        // 先做容量守卫：前滚(1200ms)+录音最长15s 之和可能超过 utter_cap，
        // memcpy 越界会写坏 PSRAM 堆（症状：串口死、HTTP 异常，都是它）
        if (g_utter_len + n > g_utter_cap) {
            capture = false;
            g_state = ST_IDLE;
            logf("[rec] 达到上限强制断句 %.1fs", capture_ms / 1000.0f);
            handle_utterance();
            g_state = ST_IDLE;
            ledState(g_state);
            return;
        }
        memcpy(g_utter + g_utter_len, mono, n * sizeof(int16_t));
        g_utter_len += n;
        capture_ms += FRAME_MS;
        bool btn_held = digitalRead(BUTTON_PIN) == LOW;
        bool quiet = db < VAD_END_DB;
        silence_ms = quiet ? silence_ms + FRAME_MS : 0;
        bool done = false;
        if (TRIGGER_VAD && silence_ms >= END_SILENCE_MS) done = true;      // VAD 断句
        if (!TRIGGER_VAD && !btn_held && capture_ms > 400) done = true;    // 松开
        if (done) {
            capture = false;
            g_state = ST_IDLE;
            logf("[rec] 结束 %.1fs（%d 采样）", capture_ms / 1000.0f,
                 (int)g_utter_len);
            handle_utterance();
            g_state = ST_IDLE;
            ledState(g_state);
        }
    }
}
