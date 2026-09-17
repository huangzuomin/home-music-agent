#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Home Music Agent — 语音入口引擎（需求文档 v0.3 §6-§9）。

设计目标：把「按住说话」换成「喊一声就说话」。

    WAITING_WAKE → (唤醒词) → LISTENING → (VAD 判完) → 回调一段完整语音

四条硬约束（都来自实测踩坑，改代码时别动）：

1. **只允许一个 AudioCapture 实例**（§6.4）。Windows 的音频共享很脆，
   多开 ffmpeg 抓同一个 dshow 设备会互相抢，蓝牙 HFP 还会反复切换。
   唤醒词模块**绝不自己开麦克风**，一律从这个实例的帧流里取数据。

2. **必须边采边读（stdout 管道）**。旧的 Recorder 是「录到文件、退出后才读」，
   那种模式根本做不了实时唤醒/VAD。停止时仍然要给 ffmpeg 的 stdin 送 `q`，
   否则拿不到尾部缓冲（`terminate()` 会丢数据）。

3. **RingBuffer 必须有**（§6.5）。用户常连着说「Hey Jarvis 给我来点爵士」，
   唤醒词判定本身有延迟，没有回填缓冲就会吞掉句首。

4. **TTS 播报期间必须暂停唤醒检测**（§9.3），否则系统会听见自己的声音而自唤醒。
   恢复后还要再等 guard_ms 才真正启用。
"""
from __future__ import annotations

import collections
import math
import os
import re
import struct
import subprocess
import threading
import time
from enum import Enum
from typing import Any, Callable, Iterator


# ------------------------------------------------------------------ 状态

class VoiceState(str, Enum):
    """§8 语音状态机。值直接是给人看的中文，UI 可以直接显示。"""

    OFFLINE = "离线"
    WAITING_WAKE = "等待唤醒"
    WAKE_DETECTED = "已唤醒"
    LISTENING = "正在听"
    TRANSCRIBING = "正在识别"
    AGENT_RUNNING = "Agent 正在处理"
    RESPONDING = "正在播报"
    ERROR = "错误"


# ------------------------------------------------------------------ 音频采集

class AudioCapture:
    """单实例常驻采集：ffmpeg + DirectShow → stdout 裸 PCM（16kHz/1ch/s16le）。

    产出固定长度的帧（默认 20ms = 320 采样 = 640 字节），供唤醒/VAD/录音共用。
    """

    READY_MARK = "Press [q] to stop"

    def __init__(self, ffmpeg: str, device: str, frame_ms: int = 20,
                 sample_rate: int = 16000) -> None:
        self.ffmpeg = ffmpeg
        self.device = device
        self.frame_ms = frame_ms
        self.sample_rate = sample_rate
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self.ready = threading.Event()
        self.stderr_lines: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    # -------------------------------------------------- 生命周期

    def start(self) -> None:
        cmd = [
            self.ffmpeg, "-hide_banner",
            # ⚠️ 必须是 info 级别：设备就绪信号就是 ffmpeg 打印的
            #    「Press [q] to stop」。用 -loglevel warning 会把它一起滤掉，
            #    导致 wait_ready() 永远超时（实测踩过）。-nostats 只是去掉进度行。
            "-loglevel", "info", "-nostats",
            "-f", "dshow", "-i", "audio=%s" % self.device,
            "-ac", "1", "-ar", str(self.sample_rate), "-sample_fmt", "s16",
            # 不设 -t：常驻采集，直到我们主动送 q
            "-f", "s16le", "-",
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self._reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr_lines.append(line)
                    del self.stderr_lines[:-40]
                if self.READY_MARK in line:
                    self.ready.set()
        except Exception:
            pass
        finally:
            self.ready.set()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """等 ffmpeg 把 dshow 设备打开（约 0.4~0.6s）。"""
        return self.ready.wait(timeout=timeout)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"q")      # 必须送 q，否则丢尾部缓冲
                proc.stdin.flush()
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
        if self._reader is not None:
            self._reader.join(timeout=1.5)

    # -------------------------------------------------- 读帧

    def frames(self) -> Iterator[bytes]:
        """持续产出定长帧；进程结束或出错则停止迭代。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buf = b""
        while True:
            try:
                chunk = proc.stdout.read(self.frame_bytes - len(buf))
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= self.frame_bytes:
                yield buf[:self.frame_bytes]
                buf = buf[self.frame_bytes:]

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# ------------------------------------------------------------------ 工具


def frame_dbfs(frame: bytes) -> float:
    """帧的 RMS 电平（dBFS）。静音约 -90，正常说话约 -35~-15。"""
    n = len(frame) // 2
    if n == 0:
        return -120.0
    total = 0
    for i in range(0, n, 4):        # 抽样足够
        v = struct.unpack_from("<h", frame, i * 2)[0]
        total += v * v
    rms = math.sqrt(total / max(1, n // 4))
    if rms < 1e-9:
        return -120.0
    return 20.0 * math.log10(rms / 32768.0)


class RingBuffer:
    """保留最近 N 毫秒音频（§6.5），唤醒后回填到语音开头。"""

    def __init__(self, ms: int, frame_bytes: int, frame_ms: int) -> None:
        self._max = max(1, int(ms / max(1, frame_ms)))
        self._frames: collections.deque[bytes] = collections.deque(maxlen=self._max)

    def push(self, frame: bytes) -> None:
        self._frames.append(frame)

    def snapshot(self) -> list[bytes]:
        return list(self._frames)

    def clear(self) -> None:
        self._frames.clear()

    @property
    def capacity_ms(self) -> int:
        return self._max


class EnergyVAD:
    """基于能量的语音活动检测，带自适应噪声底。

    不用 webrtcvad：它在 Windows 上常需要编译工具链，而本场景（近距离麦克风、
    安静房间）能量法已经足够，且零额外依赖。

    §7.3 的 ``speech_start_threshold`` 解释为「归一化电平」（-60dBFS→0，0dBFS→1）。
    文档给的 0.5（=-30dBFS）**实测偏严**：2026-09-13 在 DJI Mic 蓝牙 HFP 通道上量到
    环境噪声约 -61dBFS、说话约 -32dBFS，0.5 会把正常说话卡在门外。故默认降到 0.35
    （≈-39dBFS）。真正的判据其实是第二个条件（高于噪声底 6dB），绝对阈值只用来挡掉
    「安静环境里的微小波动」。
    """

    def __init__(self, start_threshold: float = 0.35, end_silence_ms: int = 800,
                 speech_timeout_sec: float = 5.0, max_utterance_sec: float = 15.0,
                 frame_ms: int = 20) -> None:
        self.start_threshold = start_threshold
        self.end_silence_ms = end_silence_ms
        self.speech_timeout_sec = speech_timeout_sec
        self.max_utterance_sec = max_utterance_sec
        self.frame_ms = frame_ms
        self._noise_db = -60.0

    def level(self, frame: bytes) -> float:
        """归一化电平 0~1。"""
        db = frame_dbfs(frame)
        return max(0.0, min(1.0, (db + 60.0) / 60.0))

    def observe_noise(self, frame: bytes) -> None:
        """静默期更新噪声底（慢速跟随，避免把语音算进噪声）。"""
        db = frame_dbfs(frame)
        if db < self._noise_db + 6.0:
            self._noise_db = 0.98 * self._noise_db + 0.02 * db

    @property
    def noise_db(self) -> float:
        return self._noise_db

    def is_speech(self, frame: bytes) -> bool:
        """同时满足「绝对阈值」与「高于噪声底一定余量」，才算说话。"""
        db = frame_dbfs(frame)
        if self.level(frame) < self.start_threshold:
            return False
        return db > self._noise_db + 6.0


# ------------------------------------------------------------------ 唤醒词

class WakeWord:
    """openWakeWord 本地唤醒词检测（§6.3）。

    两个必须遵守的接口约束：
      * 输入必须是 **int16 单声道 16kHz** 的 numpy 数组；
      * 每次喂 **1280 采样（80ms）**——openWakeWord 内部按 80ms 切片，
        长度不对会报错或给出错误分数。
    """

    CHUNK_SAMPLES = 1280          # 80ms @16kHz
    CHUNK_BYTES = CHUNK_SAMPLES * 2

    def __init__(self, model_name: str = "hey_jarvis",
                 threshold: float = 0.6, frame_ms: int = 20) -> None:
        import numpy as np                      # 延迟导入：没装时由调用方降级
        from openwakeword.model import Model

        self._np = np
        self.model_name = model_name
        self.threshold = threshold
        self._model = Model(wakeword_models=[model_name],
                            inference_framework="onnx")
        self._buf = b""
        self._frame_ms = frame_ms
        self.last_score = 0.0

    def feed(self, frame: bytes) -> float:
        """喂一帧（20ms），返回当前唤醒分数（未凑满 80ms 时返回上次分数）。"""
        self._buf += frame
        score = self.last_score
        while len(self._buf) >= self.CHUNK_BYTES:
            chunk, self._buf = self._buf[:self.CHUNK_BYTES], self._buf[self.CHUNK_BYTES:]
            arr = self._np.frombuffer(chunk, dtype=self._np.int16)
            preds = self._model.predict(arr)
            if preds:
                score = max(float(v) for v in preds.values())
        self.last_score = score
        return score

    def reset(self) -> None:
        """清空内部状态，避免上一轮的尾巴立刻再次触发。"""
        self._buf = b""
        self.last_score = 0.0
        try:
            self._model.reset()
        except Exception:
            pass


# ------------------------------------------------------------------ 引擎

class VoiceEngine:
    """把采集 / 唤醒 / VAD / 状态机串起来，跑在后台线程。

    对外只通过三个回调暴露：
        on_state(VoiceState, detail)
        on_utterance(pcm_bytes, meta)     ← 一段完整语音（已含 RingBuffer 回填）
        on_log(str)
    """

    def __init__(self, cfg: dict[str, Any], ffmpeg: str,
                 on_state: Callable[[VoiceState, str], None] | None = None,
                 on_utterance: Callable[[bytes, dict], None] | None = None,
                 on_log: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.on_state = on_state or (lambda s, d="": None)
        self.on_utterance = on_utterance or (lambda pcm, meta: None)
        self.on_log = on_log or (lambda msg: None)

        v = cfg.get("voice", {}) or {}
        vad = v.get("vad", {}) or {}
        self.mode = v.get("mode", "wake_word")
        self.wake_word_name = v.get("wake_word", "hey_jarvis")
        self.wake_threshold = float(v.get("wake_threshold", 0.6))
        self.ring_buffer_ms = int(v.get("ring_buffer_ms", 1200))
        self.tts_guard_ms = int((v.get("tts", {}) or {}).get("guard_ms", 300))
        self.follow_up_window_sec = float(
            (cfg.get("conversation", {}) or {}).get("follow_up_window_sec", 15))
        self.frame_ms = int(v.get("frame_ms", 20))

        self.vad = EnergyVAD(
            start_threshold=float(vad.get("speech_start_threshold", 0.5)),
            end_silence_ms=int(vad.get("end_silence_ms", 800)),
            speech_timeout_sec=float(vad.get("speech_timeout_sec", 5)),
            max_utterance_sec=float(vad.get("max_utterance_sec", 15)),
            frame_ms=self.frame_ms,
        )
        self.ring = RingBuffer(self.ring_buffer_ms, 0, self.frame_ms)

        self.capture: AudioCapture | None = None
        self.wake: WakeWord | None = None
        self.wake_error = ""
        self._state = VoiceState.OFFLINE
        self._loop_state = VoiceState.WAITING_WAKE
        self._thread: threading.Thread | None = None
        self._running = False
        self._wake_enabled = True
        self._wake_resume_at = 0.0
        # pause_wake() 的时刻。用于 fail-safe：客户端只 pause 不 resume（历史 bug）
        # 或 TTS 卡死时，超过 wake_pause_max_sec 强制恢复，避免永久失聪。
        self._wake_paused_at = 0.0
        self.wake_pause_max_sec = float(v.get("wake_pause_max_sec", 30))
        self._last_wake_at = 0.0
        # 一轮对话的处理结果还没回来（STT/Agent/TTS 进行中）→ 暂停检测
        self._busy = False
        # §19：一次唤醒后的连续对话窗口，窗口内不必再次喊唤醒词
        self._follow_up_until = 0.0
        self._pending: list[bytes] = []
        self._silence_ms = 0
        self._speech_ms = 0
        self._wait_deadline = 0.0
        self.sample_rate = 16000
        self._stats = {"wake_hits": 0, "utterances": 0, "frames": 0,
                       "forced_wake_resume": 0,
                       "wake_deaf_errors": 0}

    # -------------------------------------------------- 状态

    @property
    def state(self) -> VoiceState:
        return self._state

    def _set_state(self, st: VoiceState, detail: str = "") -> None:
        if st != self._state or detail:
            self._state = st
            try:
                self.on_state(st, detail)
            except Exception:
                pass

    def set_state(self, st: VoiceState, detail: str = "") -> None:
        """供外部（工作线程）推进 UI 状态，如 TRANSCRIBING / AGENT_RUNNING。"""
        self._set_state(st, detail)

    # -------------------------------------------------- TTS Guard

    def pause_wake(self) -> None:
        """TTS 开始播报时调用（§9.3）。"""
        self._wake_enabled = False
        self._wake_resume_at = 0.0
        self._wake_paused_at = time.time()

    def resume_wake(self, guard_ms: int | None = None) -> None:
        """TTS 结束后调用；延迟 guard_ms 才真正恢复唤醒检测。"""
        guard = self.tts_guard_ms if guard_ms is None else guard_ms
        self._wake_resume_at = time.time() + guard / 1000.0
        if self.wake is not None:
            self.wake.reset()

    def _force_resume_wake(self, why: str) -> None:
        """无条件恢复唤醒（含 fail-safe）。

        ★ 2026-09-13 修复：**这是「唤醒词只在第一轮有效」的根因**。
        客户端（run_voice_loop / GUI）在 TTS 前调用 pause_wake()，但**从不调用
        resume_wake()**，而 finish_turn() 又没把 _wake_enabled 置回 True。
        结果：主循环每帧都卡在闸门②（`if not self._wake_enabled`）里 continue，
        而 `_wake_resume_at == 0` 使自动恢复条件永远为假 ——
        **第一次 TTS 播完之后，引擎永久失聪，唤醒词再也点不动。**
        """
        self._wake_enabled = True
        self._wake_resume_at = 0.0
        self._wake_paused_at = 0.0
        if self.wake is not None:
            self.wake.reset()
        if self._stats.get("forced_wake_resume") is not None:
            self._stats["forced_wake_resume"] += 1
        self.on_log("🔊 唤醒检测已恢复（%s）" % why)

    # -------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._running:
            return
        device = (self.cfg.get("device") or "").strip()
        if not device:
            self._set_state(VoiceState.ERROR, "未配置麦克风设备")
            return
        self.capture = AudioCapture(self.ffmpeg, device, frame_ms=self.frame_ms)
        self.capture.start()
        if not self.capture.wait_ready(5.0):
            tail = " / ".join(self.capture.stderr_lines[-2:])
            self._set_state(VoiceState.ERROR, "麦克风启动失败：%s" % tail)
            self.capture.stop()
            self.capture = None
            return

        if self.mode == "wake_word":
            try:
                self.wake = WakeWord(self.wake_word_name, self.wake_threshold,
                                     frame_ms=self.frame_ms)
                self.wake_error = ""
                self.on_log("唤醒词已加载：%s（阈值 %.2f）" % (
                    self.wake_word_name, self.wake_threshold))
            except Exception as exc:
                self.wake = None
                self.wake_error = "%s: %s" % (type(exc).__name__, exc)
                self.on_log("⚠️ 唤醒词不可用（%s）→ 退化为按住说话" % self.wake_error)

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._set_state(VoiceState.WAITING_WAKE)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        self._set_state(VoiceState.OFFLINE)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # -------------------------------------------------- 主循环

    def _enter_listening(self, wait_sec: float, use_ring: bool) -> None:
        """进入「正在听」。use_ring=True 时回填 RingBuffer（§6.5 防吞句首）。"""
        self._pending = self.ring.snapshot() if use_ring else []
        self._silence_ms = 0
        self._speech_ms = 0
        self._wait_deadline = time.time() + wait_sec
        self._loop_state = VoiceState.LISTENING
        self._set_state(VoiceState.LISTENING)

    def finish_turn(self) -> None:
        """一轮对话彻底结束（STT / Agent / TTS 都完成）后由外部调用。

        若配置了 ``follow_up_window_sec``，则保持「正在听」——
        用户可以在窗口内直接接着说「这个不错」，不必再喊唤醒词（§19）。

        ⚠️ 这里**必须**把唤醒闸门放回去：客户端只调 ``pause_wake()``（TTS 防自唤醒），
        没人调 ``resume_wake()``。不恢复就等于「一轮之后永久失聪」——
        实测就是这么坏掉的（见 ``_force_resume_wake`` 的注释）。
        """
        self._busy = False
        self._force_resume_wake("一轮对话结束")
        if self.follow_up_window_sec > 0:
            self._follow_up_until = time.time() + self.follow_up_window_sec
            self._enter_listening(self.follow_up_window_sec, use_ring=False)
            self.on_log("↩︎ 连续对话窗口 %.0fs 内可直接接着说"
                        % self.follow_up_window_sec)
        else:
            self._loop_state = VoiceState.WAITING_WAKE
            self._set_state(VoiceState.WAITING_WAKE)

    def _resume_after_tts(self) -> None:
        """TTS 播报结束且保护期已过（§9.3）。"""
        if time.time() < self._follow_up_until:
            self._enter_listening(self.follow_up_window_sec, use_ring=False)
        else:
            self._loop_state = VoiceState.WAITING_WAKE
            self._set_state(VoiceState.WAITING_WAKE)

    def _finalize(self, reason: str) -> None:
        """一段语音结束 → 交给外部处理（STT → Agent → TTS）。"""
        pcm = b"".join(self._pending)
        self._pending = []
        self._silence_ms = self._speech_ms = 0
        if len(pcm) < self.sample_rate // 20:       # <50ms 当作噪声丢弃
            self.on_log("（语音过短，忽略）")
            self._loop_state = VoiceState.WAITING_WAKE
            self._set_state(VoiceState.WAITING_WAKE)
            return
        self._stats["utterances"] += 1
        self.on_log("🎤 收到 %.2fs 语音（%s）" % (
            len(pcm) / 2.0 / self.sample_rate, reason))
        self._busy = True
        self._set_state(VoiceState.TRANSCRIBING)
        try:
            self.on_utterance(pcm, {"reason": reason})
        except Exception as exc:
            self.on_log("处理语音失败：%s" % exc)
            self.finish_turn()

    def _loop(self) -> None:
        assert self.capture is not None

        for frame in self.capture.frames():
            if not self._running:
                break
            self._stats["frames"] += 1

            # ---- 闸门①：一轮对话还在处理（STT/Agent/TTS）→ 完全不看音频。
            #     必须如此：否则会把 TTS 自己的声音当用户说话，
            #     或把上一轮的尾巴当成新句子。
            if self._busy:
                continue

            # ---- 闸门②：TTS 播报保护期（§9.3）
            if not self._wake_enabled:
                if self._wake_resume_at and time.time() >= self._wake_resume_at:
                    self._force_resume_wake("TTS 保护期结束")
                    self._resume_after_tts()
                elif (self._wake_paused_at
                      and time.time() - self._wake_paused_at > self.wake_pause_max_sec):
                    # fail-safe：客户端调了 pause_wake 却因为异常/卡死没走到
                    # finish_turn → 不能让它永久失聪（实测就是这么坏的）
                    self.on_log("⚠️ 暂停唤醒超过 %.0fs 未恢复，强制恢复"
                                % self.wake_pause_max_sec)
                    self._force_resume_wake("fail-safe 超时")
                    self._resume_after_tts()
                if not self._wake_enabled:
                    continue

            st = self._loop_state

            # ---- 等待唤醒
            if st == VoiceState.WAITING_WAKE:
                self.ring.push(frame)
                if self.wake is None:
                    self.vad.observe_noise(frame)   # 无唤醒词引擎：只跟噪声底
                    continue
                score = self.wake.feed(frame)
                if score >= self.wake_threshold:
                    self._stats["wake_hits"] += 1
                    self._last_wake_at = time.time()
                    self.on_log("🔔 唤醒（score=%.2f）" % score)
                    self._set_state(VoiceState.WAKE_DETECTED)
                    self._enter_listening(self.vad.speech_timeout_sec,
                                          use_ring=True)
                continue

            # ---- 正在听：VAD 判起止
            if st == VoiceState.LISTENING:
                self.ring.push(frame)       # 保持缓冲新鲜，供下一轮回填
                if self.vad.is_speech(frame):
                    self._pending.append(frame)
                    self._speech_ms += self.frame_ms
                    self._silence_ms = 0
                else:
                    self.vad.observe_noise(frame)
                    if self._speech_ms > 0:
                        self._pending.append(frame)     # 静音也收，保住句尾
                        self._silence_ms += self.frame_ms

                elapsed_ms = len(self._pending) * self.frame_ms
                if (self._speech_ms > 0
                        and self._silence_ms >= self.vad.end_silence_ms):
                    self._finalize("静音结束")
                elif elapsed_ms >= self.vad.max_utterance_sec * 1000:
                    self._finalize("超长截断")
                elif self._speech_ms == 0 and time.time() >= self._wait_deadline:
                    self.on_log("⏱ 等待超时（未听到说话），回到待唤醒")
                    self._loop_state = VoiceState.WAITING_WAKE
                    self._set_state(VoiceState.WAITING_WAKE)
                    if self.wake is not None:
                        self.wake.reset()
                continue

        self._set_state(VoiceState.OFFLINE)

    # -------------------------------------------------- 供 PTT 复用

    def push_ptt(self, pcm: bytes) -> None:
        """按住说话模式：外部拿到 PCM 后走同一条回调链路。"""
        self._stats["utterances"] += 1
        self._set_state(VoiceState.TRANSCRIBING)
        try:
            self.on_utterance(pcm, {"reason": "ptt"})
        except Exception as exc:
            self.on_log("处理语音失败：%s" % exc)


# ------------------------------------------------------------------ 配置默认值

DEFAULT_VOICE_CONFIG: dict[str, Any] = {
    "voice": {
        "mode": "wake_word",            # wake_word | push_to_talk
        "wake_word": "hey_jarvis",
        # 文档 §20 建议 0.60。但 hey_jarvis 是「英文母语者 + 宽带音频」训练的，
        # 本机是**蓝牙 HFP 窄带 + 中文口音**，实测分数天然低一大截，
        # 0.60 常常怎么喊都上不去（验收清单 §5 也提到 0.2~0.55 是常见区间）。
        # 误唤醒的代价很低（后续 VAD 没听到说话就自动回到待唤醒），
        # 所以默认放宽到 0.45；用 calibrate 工具按真人实测分数定值最准。
        "wake_threshold": 0.45,
        # pause_wake() 后最长允许静默多久；超时强制恢复（防永久失聪）
        "wake_pause_max_sec": 30,
        "ring_buffer_ms": 1200,
        "frame_ms": 20,
        "vad": {
            # 文档 §7.3 建议 0.50，实测（蓝牙 HFP 通道）偏严会漏检，
            # 降到 0.35 ≈ -39dBFS；实际判据主要靠「高于噪声底 6dB」。
            "speech_start_threshold": 0.35,
            "end_silence_ms": 800,
            "speech_timeout_sec": 5,
            "max_utterance_sec": 15,
        },
        "tts": {"enabled": True, "minimal_reply": True, "guard_ms": 300},
    },
    "conversation": {"follow_up_window_sec": 15},
    "agent": {
        "context_tracks": 20,
        "conversation_turns": 10,
        "enable_music_plan": True,
        "enable_feedback": True,
        "enable_memory": False,
    },
}


def merge_voice_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """把 DEFAULT_VOICE_CONFIG 深度合并进 cfg（不覆盖用户已设的值）。"""
    out = dict(cfg)
    for key, default in DEFAULT_VOICE_CONFIG.items():
        if not isinstance(default, dict):
            out.setdefault(key, default)
            continue
        cur = out.get(key)
        if not isinstance(cur, dict):
            out[key] = dict(default)
            continue
        merged = dict(default)
        for k, v in cur.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                sub = dict(merged[k])
                sub.update(v)
                merged[k] = sub
            else:
                merged[k] = v
        out[key] = merged
    return out
