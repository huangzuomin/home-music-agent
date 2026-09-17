"""IMP-12 补丁：给固件加真实闭麦（muted 状态/LED/loop 闸门/长按切换）。

对 open-source 库的 main.cpp 应用四处补丁（锚点不匹配即报错停止）。
"""
from __future__ import annotations

import sys

p = "voice-client/xvf3800-satellite/src/main.cpp"
s = open(p, encoding="utf-8").read()

# ① 单例声明（挂到 wake_ok 之后）
old = """static bool g_wifi_ok = false;
static bool wake_ok = false;          // 唤醒词引擎是否可用"""
new = """static bool g_wifi_ok = false;
static bool wake_ok = false;          // 唤醒词引擎是否可用
static bool g_muted = false;          // IMP-12：真实闭麦（输入端停止采集）"""
assert old in s, "patch1 decl anchor"
s = s.replace(old, new, 1)

# ② ledState：muted 优先（红色实心）
old = """static void ledState(SatState st) {
    switch (st) {"""
new = """static void ledState(SatState st) {
    if (g_muted) {
        ledSet(120, 0, 0);            // 红色实心 = 已闭麦
        return;
    }
    switch (st) {"""
assert old in s, "patch2 led anchor"
s = s.replace(old, new, 1)

# ③ loop 开头：闭麦闸门
old = """void loop() {
    if (WiFi.status() != WL_CONNECTED) {"""
new = """void loop() {
    if (g_muted) {
        // IMP-12（T15）：真实闭麦——输入端彻底停止采集与上传
        ledSet(120, 0, 0);
        delay(100);
        return;
    }
    if (WiFi.status() != WL_CONNECTED) {"""
assert old in s, "patch3 loop anchor"
s = s.replace(old, new, 1)

# ④ 长按 2s 切换闭麦（两处 i2s_read 调用点：单元自检 + 主循环，取主循环那处）
old = """    static int16_t mono[FRAME_MS * SAMPLE_RATE / 1000];
    size_t n = i2s_read_frame_mono(mono);
    if (!n) return;
    float db = frame_dbfs(mono, n);"""
new = """    static int16_t mono[FRAME_MS * SAMPLE_RATE / 1000];
    size_t n = i2s_read_frame_mono(mono);
    if (!n) return;
    float db = frame_dbfs(mono, n);

    // IMP-12：长按 2s = 切换闭麦（与短按触发区分；闭麦时彻底停止采集上传）
    static uint32_t btn_ms = 0;
    static bool long_fired = false;
    if (digitalRead(BUTTON_PIN) == LOW) {
        if (btn_ms == 0) btn_ms = millis();
        if (!long_fired && millis() - btn_ms >= 2000) {
            long_fired = true;
            g_muted = !g_muted;
            logf("[mute] %s", g_muted ? "已闭麦（输入端停止采集）"
                                       : "已恢复拾音");
            ledState(g_state);
            return;
        }
    } else {
        btn_ms = 0;
        long_fired = false;
    }"""
count = s.count(old)
assert count >= 1, "patch4 read anchor"
s = s.replace(old, new, 1)               # 只替换主循环里第一处
open(p, "w", encoding="utf-8").write(s)
print(f"applied 4 patches (read-site occurrences: {count})")
