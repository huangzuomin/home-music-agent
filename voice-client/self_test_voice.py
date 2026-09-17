#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音引擎离线自测（不依赖 openWakeWord）。

分四段：
  1. RingBuffer  —— 容量与快照顺序
  2. EnergyVAD   —— 电平计算、噪声底、语音判定
  3. AudioCapture—— **真实**打开 DJI Mic，读 3 秒并报告电平（验证常驻管道可行）
  4. 状态机      —— 用假设备/假唤醒词注入「唤醒→说话→静音」，验证自动断句

用法：python self_test_voice.py            # 全跑（会占用麦克风 3 秒）
      python self_test_voice.py --no-mic   # 跳过真实麦克风那段
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_engine as ve  # noqa: E402
import _console  # noqa: E402


def tone_frame(rms_db: float, frame_bytes: int = 640) -> bytes:
    """生成指定 RMS 电平的帧（正弦波）。"""
    n = frame_bytes // 2
    amp = 32768 * (10 ** (rms_db / 20.0)) * math.sqrt(2)
    amp = min(amp, 32000)
    out = bytearray()
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * 220 * i / 16000))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


def silence_frame(frame_bytes: int = 640) -> bytes:
    return b"\x00" * frame_bytes


# ------------------------------------------------------------- 1. RingBuffer

def test_ring_buffer() -> bool:
    print("=== 1. RingBuffer ===")
    rb = ve.RingBuffer(ms=200, frame_bytes=640, frame_ms=20)
    print("  容量 = %d 帧（应为 10）" % rb.capacity_ms)
    for i in range(25):
        rb.push(struct.pack("<h", i))
    snap = rb.snapshot()
    first = struct.unpack("<h", snap[0][:2])[0]
    last = struct.unpack("<h", snap[-1][:2])[0]
    ok = len(snap) == 10 and first == 15 and last == 24
    print("  存 25 帧后剩 %d 帧，首=%d 尾=%d → %s" % (
        len(snap), first, last, "[OK]" if ok else "[FAIL]"))
    rb.clear()
    print("  clear 后 = %d 帧 %s" % (len(rb.snapshot()), "[OK]" if not rb.snapshot() else "[FAIL]"))
    return ok


# ------------------------------------------------------------- 2. VAD

def test_vad() -> bool:
    print("\n=== 2. EnergyVAD ===")
    vad = ve.EnergyVAD(start_threshold=0.5, end_silence_ms=800)
    quiet, loud = silence_frame(), tone_frame(-25.0)
    print("  静音帧: %.1f dBFS → level=%.2f speech=%s" % (
        ve.frame_dbfs(quiet), vad.level(quiet), vad.is_speech(quiet)))
    print("  语音帧: %.1f dBFS → level=%.2f speech=%s" % (
        ve.frame_dbfs(loud), vad.level(loud), vad.is_speech(loud)))
    for _ in range(60):
        vad.observe_noise(quiet)
    print("  噪声底跟随 60 帧静音后 = %.1f dBFS" % vad.noise_db)
    ok = (not vad.is_speech(quiet)) and vad.is_speech(loud)
    print("  判定正确 → %s" % ("[OK]" if ok else "[FAIL]"))
    return ok


# ------------------------------------------------------------- 3. 真实麦克风

def test_capture(device: str) -> bool:
    print("\n=== 3. AudioCapture（真实设备）===")
    print("  设备：%s" % device)
    try:
        import voice_client as vc
        ffmpeg = vc.find_ffmpeg()
    except Exception as exc:
        print("  [FAIL] 找不到 ffmpeg：%s" % exc)
        return False
    cap = ve.AudioCapture(ffmpeg, device, frame_ms=20)
    cap.start()
    if not cap.wait_ready(6.0):
        print("  [FAIL] 设备未就绪：%s" % " / ".join(cap.stderr_lines[-2:]))
        cap.stop()
        return False
    print("  [OK] 设备已就绪（ffmpeg 已打开 dshow）")

    levels, n = [], 0
    t0 = time.time()
    try:
        for frame in cap.frames():
            levels.append(ve.frame_dbfs(frame))
            n += 1
            if time.time() - t0 >= 3.0:
                break
    finally:
        cap.stop()
    if not levels:
        print("  [FAIL] 没读到任何帧")
        return False
    secs = n * 0.02
    avg = sum(levels) / len(levels)
    peak = max(levels)
    print("  读到 %d 帧（%.2fs，期望约 150）" % (n, secs))
    print("  平均 %.1f dBFS / 峰值 %.1f dBFS" % (avg, peak))
    # 每 0.5 秒一段，看是否有起伏（说话时应该跳）
    seg = max(1, len(levels) // 6)
    print("  分段均值：" + " ".join(
        "%.0f" % (sum(levels[i:i + seg]) / len(levels[i:i + seg]))
        for i in range(0, len(levels), seg)))
    ok = n > 100 and peak > -60
    print("  常驻管道可用 → %s" % ("[OK]" if ok else "[FAIL]（帧数不足或电平过低）"))
    if peak < -60:
        print("  [!] 电平极低：麦克风可能没开/没配对，或设备名不对")
    return ok


# ------------------------------------------------------------- 4. 状态机

class FakeCapture:
    """按脚本产出帧：静音 → 语音 → 静音，用于验证自动断句。

    ``realtime=True`` 时按 20ms 真实节拍吐帧。**涉及「一轮处理期间主循环仍在跑」
    的用例必须开它** —— 否则列表会被瞬间抽干，测试主体还没来得及调
    ``finish_turn()`` 帧就没了，表现为「只收到 1 段语音」的假失败。
    """

    def __init__(self, frames: list[bytes], frame_ms: int = 20,
                 realtime: bool = False) -> None:
        self._frames = frames
        self.frame_ms = frame_ms
        self.realtime = realtime
        self.stopped = False

    def frames(self):
        for f in self._frames:
            if self.stopped:
                break
            if self.realtime:
                time.sleep(self.frame_ms / 1000.0)
            yield f

    def stop(self) -> None:
        self.stopped = True

    def alive(self) -> bool:
        return not self.stopped


class FakeWake:
    """前 3 帧返回高分（模拟唤醒），之后低分。"""

    def __init__(self, threshold: float = 0.6) -> None:
        self.threshold = threshold
        self.n = 0

    def feed(self, frame: bytes) -> float:
        self.n += 1
        return 0.95 if self.n <= 3 else 0.05

    def reset(self) -> None:
        self.n = 0


def test_state_machine() -> bool:
    print("\n=== 4. 状态机（假设备 + 假唤醒词）===")
    cfg = ve.merge_voice_defaults({"device": "fake"})
    cfg["voice"]["ring_buffer_ms"] = 200
    cfg["conversation"]["follow_up_window_sec"] = 0     # 本测先关掉，单独看断句
    states, utts, logs = [], [], []

    engine = ve.VoiceEngine(
        cfg, ffmpeg=None,
        on_state=lambda st, d="": states.append(st.value),
        on_utterance=lambda pcm, meta: utts.append((len(pcm), meta.get("reason"))),
        on_log=lambda m: logs.append(m))

    frames = [silence_frame() for _ in range(10)]          # 唤醒前
    frames += [tone_frame(-20.0) for _ in range(50)]       # 说话 1.0s
    frames += [silence_frame() for _ in range(60)]         # 静音 1.2s → 应触发结束
    engine.capture = FakeCapture(frames)
    engine.wake = FakeWake()
    engine._running = True
    th = threading.Thread(target=engine._loop, daemon=True)
    th.start()
    th.join(timeout=5)
    engine._running = False

    print("  状态轨迹：%s" % " → ".join(states))
    for m in logs:
        print("    %s" % m)
    ok = False
    if utts:
        size, reason = utts[0]
        print("  收到语音：%d 字节（%.2fs），结束原因=%s" % (
            size, size / 2 / 16000, reason))
        # 期望包含回填的 200ms + 说话的 1.0s + 结尾静音
        ok = size > 16000 * 2 * 1.0
    else:
        print("  [FAIL] 没有产生语音段")
    print("  自动断句 → %s" % ("[OK]" if ok else "[FAIL]"))
    return ok


# ------------------------------------------------------------- 5. TTS 后不失聪

def test_no_deaf_after_tts() -> bool:
    """回归：一轮对话（含 TTS）结束后，唤醒检测必须仍然可用。

    ★ 2026-09-13 实测 bug：客户端只在 TTS 前调 pause_wake()，**没人调
    resume_wake()**，finish_turn() 又不把 _wake_enabled 置回 True →
    主循环每帧都卡在闸门② 里 continue，而 _wake_resume_at==0 使自动恢复
    条件永远为假 ⇒ **第一次播报之后引擎永久失聪**，唤醒词再也点不动
    （用户报的「唤醒词无效、语音调不了音量」就是这个）。

    这里模拟「唤醒 → 说话 → 一轮结束（含 pause_wake）」两次，
    要求第二次仍能唤醒。
    """
    print("\n=== 5. 一轮结束后仍可唤醒（防「播报一次就失聪」）===")
    cfg = ve.merge_voice_defaults({"device": "fake"})
    cfg["voice"]["ring_buffer_ms"] = 200
    cfg["conversation"]["follow_up_window_sec"] = 0
    utts, logs = [], []

    engine = ve.VoiceEngine(
        cfg, ffmpeg=None,
        on_state=lambda st, d="": None,
        on_utterance=lambda pcm, meta: utts.append(meta.get("reason")),
        on_log=lambda m: logs.append(m))

    # 每个「唤醒词」持续 5 帧高分，之后走 20 帧静音 + 1.0s 说话 + 1.2s 静音
    class AlwaysWake:
        """恒定高分：只要进 WAITING_WAKE 就立刻唤醒。

        故意不用「按帧计数爆发」的写法 —— 唤醒模型**只在 WAITING_WAKE 时被喂帧**，
        帧号与调用次数对不上，测试会假失败。恒定高分对两轮的触发条件是等价的，
        而且与帧数解耦。
        """

        def feed(self, frame: bytes) -> float:
            return 0.95

        def reset(self) -> None:
            pass

    def round_frames() -> list[bytes]:
        return ([tone_frame(-20.0) for _ in range(50)]
                + [silence_frame() for _ in range(70)])

    frames = [silence_frame() for _ in range(6)] + round_frames()
    frames += [silence_frame() for _ in range(6)] + round_frames()
    engine.capture = FakeCapture(frames, realtime=True)   # ★ 必须实时节拍
    engine.wake = AlwaysWake()
    engine._running = True
    th = threading.Thread(target=engine._loop, daemon=True)
    th.start()

    # 等第一段语音出来 → 模拟客户端的 TTS 流程
    for _ in range(400):
        if utts:
            break
        time.sleep(0.02)
    engine.pause_wake()                  # 客户端在 TTS 前会做这一步
    engine.finish_turn()                 # 客户端在 finally 里做这一步
    print("  第一轮结束后 _wake_enabled=%s（旧代码在此为 False → 永久失聪）"
          % engine._wake_enabled)

    for _ in range(400):
        if len(utts) >= 2:
            break
        time.sleep(0.02)
    engine._running = False
    engine.capture.stop()
    th.join(timeout=3)

    print("  语音段：%d 段（期望 2）%s" % (len(utts), utts))
    print("  _wake_enabled 最终=%s（必须为 True）" % engine._wake_enabled)
    ok = len(utts) >= 2 and engine._wake_enabled
    print("  TTS 后仍能唤醒 → %s" % ("[OK]" if ok else "[FAIL]"))
    if not ok:
        print("    [!] 引擎在一轮对话后失聪了 —— 检查 finish_turn() 是否恢复唤醒闸门")
    return ok


def main() -> int:
    _console.setup()
    no_mic = "--no-mic" in sys.argv
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    device = ""
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                device = json.load(fh).get("device", "")
        except Exception:
            pass

    results = {
        "RingBuffer": test_ring_buffer(),
        "EnergyVAD": test_vad(),
        "状态机": test_state_machine(),
        "TTS后不失聪": test_no_deaf_after_tts(),
    }
    if no_mic:
        print("\n=== 3. AudioCapture —— 已跳过（--no-mic）===")
    else:
        results["AudioCapture"] = test_capture(device)

    print("\n" + "=" * 60)
    print("汇总：")
    for k, v in results.items():
        print("  %-14s %s" % (k, "[OK] 通过" if v else "[FAIL] 失败"))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
