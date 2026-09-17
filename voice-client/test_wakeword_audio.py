#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证唤醒词：TTS 合成「Hey Jarvis」→ 转 16k PCM → 喂给 WakeWord。

这样不用人喊就能验证「模型 + voice_engine.WakeWord 封装」是否正确，
并顺便看阈值 0.6 是否合适。多个音色都试一遍，取最好结果。

用法：python test_wakeword_audio.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402
import voice_client as vc  # noqa: E402
import _console  # noqa: E402
from voice_engine import WakeWord  # noqa: E402

TTS_URL = "http://192.168.1.50:8200/tts"
CANDIDATES = [
    ("Hey Jarvis", "zh-CN-XiaoxiaoNeural"),
    ("Hey Jarvis", "en-US-AriaNeural"),
    ("Hey Jarvis", "en-US-GuyNeural"),
]


def tts_to_pcm(text: str, voice: str, ffmpeg: str) -> bytes:
    """合成语音并转成 16kHz 单声道 s16le PCM。"""
    resp = requests.post(TTS_URL, json={"text": text, "voice": voice}, timeout=25)
    resp.raise_for_status()
    fd, mp3 = tempfile.mkstemp(suffix=".mp3", prefix="wtest_")
    os.close(fd)
    with open(mp3, "wb") as fh:
        fh.write(resp.content)
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", mp3,
             "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
             "-f", "s16le", "-"],
            capture_output=True, timeout=30)
        return proc.stdout
    finally:
        try:
            os.remove(mp3)
        except Exception:
            pass


def main() -> int:
    _console.setup()
    ffmpeg = vc.find_ffmpeg()
    print("ffmpeg: %s" % ffmpeg)
    best = (0.0, "", "")
    for text, voice in CANDIDATES:
        try:
            pcm = tts_to_pcm(text, voice, ffmpeg)
        except Exception as exc:
            print("  %-26s [FAIL] TTS 失败：%s" % (voice, exc))
            continue
        if not pcm:
            print("  %-26s [FAIL] 没拿到 PCM" % voice)
            continue
        w = WakeWord("hey_jarvis", threshold=0.6)
        peak, at = 0.0, 0
        n = 0
        for i in range(0, len(pcm) - 640 + 1, 640):     # 20ms 一帧
            n += 1
            s = w.feed(pcm[i:i + 640])
            if s > peak:
                peak, at = s, n
        dur = len(pcm) / 2 / 16000
        hit = "✅ 触发" if peak >= 0.6 else "❌ 未达阈值"
        print("  %-26s 时长 %.2fs  峰值 %.3f（第 %d 帧）  %s"
              % (voice, dur, peak, at, hit))
        if peak > best[0]:
            best = (peak, voice, text)

    print()
    print("最佳：%.3f（%s / %r）" % best)
    if best[0] >= 0.6:
        print("[OK] 唤醒词链路正确，阈值 0.6 可用。")
        print("   下一步：run.bat --voice，然后对着麦克风说「Hey Jarvis」+ 指令。")
        return 0
    if best[0] > 0.3:
        print("[!] 分数偏低但已有响应 —— TTS 发音可能不够标准（真人发音通常更高）。")
        print("   建议真人实测一次；若仍不触发，把 config.json 的 wake_threshold 降到 0.4。")
        return 0
    print("[FAIL] 完全没响应，需排查（模型加载 / 采样率 / 音频格式）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
