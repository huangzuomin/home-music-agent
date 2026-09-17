#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Home Music Agent MVP — Windows Voice Client（薄客户端）

职责边界（严格遵循实施方案 §9.2）：
    获取麦克风音频 → 判断一句话开始/结束 → 发送音频 → 显示简单状态

明确不做（留给 VM1）：
    Agent 业务逻辑 / 音乐检索 / Home Assistant 自动化 / 用户偏好长期记忆

录音用 ffmpeg + DirectShow，输出裸 PCM 再在 Python 里套 WAV 头，
这样即使 ffmpeg 被强杀，也不会产生「头损坏」的 wav。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

# 同目录内部工具：Windows 控制台编码适配（见 _console.py 里的实测说明）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _console  # noqa: E402

# ---------------------------------------------------------------- 配置

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
DEFAULT_CONFIG = {
    "stt_url": "http://192.168.1.50:8100/transcribe",
    # 旧规则网关（Phase 4/5）。v0.3 起保留为 fallback 与排障入口。
    "gateway_url": "http://192.168.1.50:8200/command",
    # v0.3 Agent 入口（Session 化，带 Music Context 与指代解析）
    "agent_url": "http://192.168.1.50:8200/agent",
    "tts_url": "http://192.168.1.50:8200/tts",
    # 固定会话 id：同一段听音乐的上下文（"这个""刚才""后面"都靠它）
    "session_id": "living-room-current",
    # Phase 5：是否让 gateway 合成语音播报并本地播放
    "tts_enabled": True,
    "device": "",
    "max_seconds": 30,
    "hotkey_enabled": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception as exc:
            print("[warn] 读取 config.json 失败：%s" % exc, file=sys.stderr)
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- ffmpeg


def find_ffmpeg() -> str:
    for cand in (
        os.environ.get("FFMPEG"),
        shutil.which("ffmpeg"),
        r"D:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ):
        if cand and os.path.exists(cand):
            return cand
    raise RuntimeError("找不到 ffmpeg，请设置环境变量 FFMPEG 指向 ffmpeg.exe")


DEVICE_RE = re.compile(r'^\[dshow[^\]]*\]\s+"(.+?)"\s+\(audio\)')


def list_audio_devices(ffmpeg: str | None = None) -> list[str]:
    """用 ffmpeg 枚举 DirectShow 音频输入设备。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    devices = []
    for line in out.splitlines():
        m = DEVICE_RE.match(line.strip())
        if m:
            name = m.group(1)
            if name not in devices:
                devices.append(name)
    return devices


# ---------------------------------------------------------------- 录音


class Recorder:
    """一次 push-to-talk 会话的录音器：输出 16 kHz / 单声道 / s16le 裸 PCM。

    两条实测得来的硬约束，改代码时别动：

    1. **必须用 stdin 送 `q` 让 ffmpeg 干净退出**。直接 ``terminate()`` 拿到的是
       **0 字节**——ffmpeg 的输出缓冲只在正常关闭时才落盘（已用 4 组对照实验确认，
       与 `-loglevel`、`-t`、输出目录都无关）。
       推论：不能加 `-nostdin`，且 stdin 必须是管道。
    2. ffmpeg 打开 DirectShow 设备要约 0.4~0.6 s。用 stderr 里的
       ``Press [q] to stop`` 作为「设备已就绪、可以说话了」的信号。
    """

    READY_MARK = "Press [q] to stop"

    def __init__(self, ffmpeg: str, device: str, max_seconds: int = 30):
        self.ffmpeg = ffmpeg
        self.device = device
        self.max_seconds = max_seconds
        self.ready = threading.Event()
        self.stderr_lines: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._path: str | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".pcm", prefix="hmvc_")
        os.close(fd)
        self._path = path
        cmd = [
            self.ffmpeg, "-hide_banner",
            "-f", "dshow", "-i", 'audio=%s' % self.device,
            "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
            "-t", str(self.max_seconds),
            "-f", "s16le", "-y", path,
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr_lines.append(line)
                if self.READY_MARK in line:
                    self.ready.set()
        except Exception:
            pass
        finally:
            self.ready.set()

    def stop(self) -> tuple[bytes, float, str]:
        """返回 (pcm_bytes, 时长秒, ffmpeg 错误摘要)。"""
        proc, path = self._proc, self._path
        self._proc, self._path = None, None
        if proc is None or path is None:
            return b"", 0.0, ""

        if not self.ready.wait(timeout=3.0):
            self.stderr_lines.append("[warn] 等待设备就绪超时")

        err = ""
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
        except Exception as exc:
            err = "写 q 失败：%s" % exc
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            err = ("%s ffmpeg 未响应 q，已强杀" % err).strip()
        if self._reader is not None:
            self._reader.join(timeout=2)

        pcm = b""
        try:
            with open(path, "rb") as fh:
                pcm = fh.read()
        except Exception as exc:
            err = ("%s 读取失败：%s" % (err, exc)).strip()
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

        if not pcm:
            tail = " / ".join(self.stderr_lines[-3:])
            err = ("%s | %s" % (err, tail)).strip(" |")
        duration = len(pcm) / 2.0 / 16000.0
        return pcm, duration, err


def pcm_to_wav(pcm: bytes, rate: int = 16000, channels: int = 1) -> bytes:
    """把裸 PCM 套上标准 WAV 头，返回完整 wav 字节。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_peak_dbfs(pcm: bytes) -> float:
    """粗算峰值电平（dBFS），用于判断麦克风是否真的有信号。"""
    if len(pcm) < 2:
        return -120.0
    n = len(pcm) // 2
    peak = 0
    for i in range(0, n, 7):  # 抽样即可
        v = struct.unpack_from("<h", pcm, i * 2)[0]
        peak = max(peak, abs(v))
    if peak == 0:
        return -120.0
    import math
    return 20.0 * math.log10(peak / 32768.0)


# ---------------------------------------------------------------- 上传


def transcribe(wav_bytes: bytes, stt_url: str, timeout: float = 120.0) -> dict:
    import requests

    files = {"file": ("ptt.wav", wav_bytes, "audio/wav")}
    t0 = time.time()
    resp = requests.post(stt_url, files=files, timeout=timeout)
    wall = time.time() - t0
    resp.raise_for_status()
    data = resp.json()
    data["wall_seconds"] = round(wall, 3)
    return data


def send_command(text: str, gateway_url: str, timeout: float = 25.0,
                 speak: bool = False) -> dict:
    """把转写文本交给 Voice Gateway（薄客户端边界：只转发，不做意图判断）。

    Phase 5：gateway 现在可能走 LLM（1-2s），超时放宽到 25s。
    speak=True 时 gateway 会顺带合成播报音频（base64 返回）。
    """
    import requests

    if not gateway_url:
        return {}
    t0 = time.time()
    resp = requests.post(gateway_url, json={"text": text, "speak": speak}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    data["gateway_seconds"] = round(time.time() - t0, 3)
    return data


def play_audio_b64(audio_b64: str, mime: str = "audio/mpeg") -> str:
    """播放 gateway 返回的 base64 音频（Windows 用 PowerShell MediaPlayer）。

    返回 "" 表示成功，否则返回错误说明。不写文件到磁盘。
    """
    import base64
    import subprocess
    import tempfile

    if not audio_b64:
        return "无音频"
    try:
        raw = base64.b64decode(audio_b64)
    except Exception as exc:
        return "base64 解码失败：%s" % exc

    suffix = ".mp3" if "mpeg" in mime or "mp3" in mime else ".wav"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="hmvc_say_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        ps = (
            "Add-Type -AssemblyName presentationCore;"
            "$p=New-Object System.Windows.Media.MediaPlayer;"
            "$p.Open([uri]'%s');$p.Play();"
            "Start-Sleep -Milliseconds 300;"
            "$d=$p.NaturalDuration.TimeSpan.TotalSeconds;"
            "if(-not $d){$d=4};"
            "Start-Sleep -Milliseconds ([int]($d*1000));"
            "$p.Close()" % path.replace("\\", "/")
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=30,
        )
        return ""
    except Exception as exc:
        return "播放失败：%s" % exc
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def play_audio_bytes(raw: bytes, mime: str = "audio/mpeg",
                     timeout: float = 40.0) -> str:
    """用 PowerShell MediaPlayer 播放一段音频字节。返回 "" 表示成功。"""
    import subprocess
    import tempfile

    if not raw:
        return "无音频"
    suffix = ".mp3" if "mpeg" in mime or "mp3" in mime else ".wav"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="hmvc_say_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        ps = (
            "Add-Type -AssemblyName presentationCore;"
            "$p=New-Object System.Windows.Media.MediaPlayer;"
            "$p.Open([uri]'%s');$p.Play();"
            "Start-Sleep -Milliseconds 300;"
            "$d=$p.NaturalDuration.TimeSpan.TotalSeconds;"
            "if(-not $d){$d=4};"
            "Start-Sleep -Milliseconds ([int]($d*1000));"
            "$p.Close()" % path.replace("\\", "/")
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=timeout,
        )
        return ""
    except Exception as exc:
        return "播放失败：%s" % exc
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def play_audio_b64(audio_b64: str, mime: str = "audio/mpeg") -> str:
    """播放 gateway 返回的 base64 音频。返回 "" 表示成功。"""
    import base64

    if not audio_b64:
        return "无音频"
    try:
        raw = base64.b64decode(audio_b64)
    except Exception as exc:
        return "base64 解码失败：%s" % exc
    return play_audio_bytes(raw, mime)


def send_agent(text: str, agent_url: str,
               session_id: str = "living-room-current",
               timeout: float = 20.0) -> dict:
    """把转写文本交给 v0.3 Agent 入口（/agent）。

    薄客户端边界不变：只转发文本 + 会话 id，不做任何意图判断。
    """
    import requests

    if not agent_url:
        return {}
    t0 = time.time()
    resp = requests.post(agent_url, json={
        "text": text, "session_id": session_id,
        "user_id": "default", "source": "voice", "want_say": True,
    }, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    data["agent_seconds"] = round(time.time() - t0, 3)
    return data


def synthesize_tts(text: str, tts_url: str, voice: str | None = None,
                   timeout: float = 20.0) -> bytes:
    """调 gateway 的 /tts 合成播报音频，返回音频字节（失败抛异常）。"""
    import requests

    if not tts_url or not text:
        return b""
    body: dict = {"text": text}
    if voice:
        body["voice"] = voice
    resp = requests.post(tts_url, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------- 常驻语音模式


def run_voice_loop(cfg: dict) -> int:
    """常驻唤醒模式（v0.3 §6-§9）：不用按键，喊一声就说话。

    链路：唤醒 → VAD 自动断句 → STT → /agent → TTS 播报（播报期间屏蔽唤醒）。
    完整状态机在 voice_engine.VoiceEngine 里，本函数只负责串接与打印。
    """
    try:
        import voice_engine as ve
    except Exception as exc:
        print("无法加载 voice_engine：%s" % exc)
        return 2

    cfg = ve.merge_voice_defaults(cfg)
    ffmpeg = find_ffmpeg()
    agent_url = (cfg.get("agent_url") or "").strip()
    stt_url = cfg["stt_url"]
    tts_url = (cfg.get("tts_url") or "").strip()
    sid = cfg.get("session_id") or "living-room-current"
    tts_on = bool(cfg.get("tts_enabled", True))
    vcfg = cfg.get("voice", {}) or {}
    vvad = vcfg.get("vad", {}) or {}

    print("=" * 74)
    print("常驻语音模式（Ctrl+C 退出）")
    print("  唤醒词    : %s（阈值 %.2f）" % (vcfg.get("wake_word"),
                                            float(vcfg.get("wake_threshold", 0.6))))
    print("  设备      : %s" % (cfg.get("device") or "(未设置)"))
    print("  RingBuffer: %sms   VAD 静音判定: %sms   最长: %ss"
          % (vcfg.get("ring_buffer_ms"), vvad.get("end_silence_ms"),
             vvad.get("max_utterance_sec")))
    print("  STT       : %s" % stt_url)
    print("  Agent     : %s" % (agent_url or "(未设置)"))
    print("  TTS       : %s" % ("开" if tts_on else "关"))
    print("  会话 id   : %s" % sid)
    print("=" * 74)

    def on_state(st, detail: str = "") -> None:
        print("[%s] %s%s" % (time.strftime("%H:%M:%S"), st.value,
                             ("（%s）" % detail) if detail else ""), flush=True)

    def on_log(msg: str) -> None:
        print("  %s" % msg, flush=True)

    engine: "ve.VoiceEngine | None" = None

    def handle(pcm: bytes, meta: dict) -> None:
        """在工作线程里跑完整链路。

        刻意不放在采音线程里：STT + Agent + TTS 要 3~5 秒，
        若阻塞读帧，ffmpeg 的 stdout 管道会写满而卡住采集。
        """
        t0 = time.time()
        try:
            engine.set_state(ve.VoiceState.TRANSCRIBING)
            try:
                stt = transcribe(pcm_to_wav(pcm), stt_url)
            except Exception as exc:
                on_log("❌ STT 失败：%s" % exc)
                return
            text = (stt.get("text") or "").strip()
            on_log("📝 识别：%s（STT %.2fs）" % (text or "(空)", time.time() - t0))
            if not text or not agent_url:
                return

            engine.set_state(ve.VoiceState.AGENT_RUNNING)
            try:
                res = send_agent(text, agent_url, sid)
            except Exception as exc:
                on_log("❌ Agent 请求失败：%s" % exc)
                return
            extra = ""
            if res.get("agent_args"):
                extra = " " + json.dumps(res["agent_args"], ensure_ascii=False)
            on_log("🤖 [%s] %s%s（%.2fs）" % (
                res.get("path") or "agent", res.get("intent"), extra,
                res.get("agent_seconds", -1)))
            if res.get("degraded"):
                on_log("   ⚠️ 已降级：%s" % res["degraded"])

            reply = (res.get("reply") or "").strip()
            if reply:
                on_log("💬 %s" % reply)
            if reply and tts_on and tts_url:
                engine.pause_wake()                     # §9.3 防自唤醒
                engine.set_state(ve.VoiceState.RESPONDING)
                try:
                    err = play_audio_bytes(synthesize_tts(reply, tts_url))
                    if err:
                        on_log("   TTS 播放失败：%s" % err)
                except Exception as exc:
                    on_log("   TTS 失败：%s" % exc)
        finally:
            engine.finish_turn()
            on_log("（本轮 %.2fs）" % (time.time() - t0))

    def spawn(pcm: bytes, meta: dict) -> None:
        threading.Thread(target=handle, args=(pcm, meta), daemon=True).start()

    engine = ve.VoiceEngine(cfg, ffmpeg, on_state=on_state,
                            on_utterance=spawn, on_log=on_log)
    engine.start()
    if engine.state == ve.VoiceState.ERROR:
        print("启动失败，退出。")
        return 2
    if engine.wake is None:
        print("[!] 唤醒词引擎不可用，常驻模式无法唤醒。")
        print("   原因：%s" % (engine.wake_error or "未知"))
        print("   请先安装：python -m pip install openwakeword")
        engine.stop()
        return 3
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止中…")
    finally:
        engine.stop()
    print("统计：%s" % engine.stats)
    return 0


# ---------------------------------------------------------------- GUI


def run_gui(cfg: dict) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    ffmpeg = find_ffmpeg()

    root = tk.Tk()
    root.title("Home Music Agent — Voice Client")
    root.geometry("560x520")
    root.minsize(480, 460)

    state = {
        "recorder": None,
        "recording": False,
        "devices": [],
        "last_pcm": b"",
        "last_duration": 0.0,
        "cfg": cfg,
    }

    # --- 顶部：STT 地址
    top = ttk.Frame(root, padding=(12, 10, 12, 4))
    top.pack(fill="x")
    ttk.Label(top, text="STT 服务").pack(side="left")
    url_var = tk.StringVar(value=cfg["stt_url"])
    ttk.Entry(top, textvariable=url_var).pack(side="left", fill="x", expand=True, padx=8)
    status_var = tk.StringVar(value="就绪")
    ttk.Label(top, textvariable=status_var, width=14, anchor="e").pack(side="right")

    # --- 指令网关（Phase 4/5）：留空则只转写、不下发音乐指令
    gw_frame = ttk.Frame(root, padding=(12, 0, 12, 4))
    gw_frame.pack(fill="x")
    ttk.Label(gw_frame, text="指令网关").pack(side="left")
    gw_var = tk.StringVar(value=cfg.get("gateway_url", ""))
    ttk.Entry(gw_frame, textvariable=gw_var).pack(side="left", fill="x", expand=True, padx=8)

    # --- Phase 5：TTS 语音回复开关
    tts_frame = ttk.Frame(root, padding=(12, 0, 12, 4))
    tts_frame.pack(fill="x")
    speak_var = tk.BooleanVar(value=bool(cfg.get("tts_enabled", True)))
    ttk.Checkbutton(tts_frame, text="语音播报回复（TTS）",
                    variable=speak_var).pack(side="left")
    ttk.Label(tts_frame, text="（关闭可省 1~2 秒）",
              foreground="#888").pack(side="left", padx=6)

    # --- 设备选择
    dev_frame = ttk.Frame(root, padding=(12, 0, 12, 6))
    dev_frame.pack(fill="x")
    ttk.Label(dev_frame, text="麦克风").pack(side="left")
    dev_var = tk.StringVar(value=cfg.get("device") or "")
    dev_combo = ttk.Combobox(dev_frame, textvariable=dev_var, state="readonly")
    dev_combo.pack(side="left", fill="x", expand=True, padx=8)

    def refresh_devices(announce: bool = True) -> None:
        try:
            devs = list_audio_devices(ffmpeg)
        except Exception as exc:
            status_var.set("设备枚举失败")
            if announce:
                messagebox.showerror("错误", str(exc))
            return
        state["devices"] = devs
        dev_combo["values"] = devs
        if devs and dev_var.get() not in devs:
            preferred = next((d for d in devs if "DJI" in d or "Wireless" in d), devs[0])
            dev_var.set(preferred)

    ttk.Button(dev_frame, text="刷新", width=6, command=refresh_devices).pack(side="right")

    # --- v0.3 常驻监听（唤醒词）：勾上就后台常驻，喊唤醒词后直接说话
    live_frame = ttk.Frame(root, padding=(12, 0, 12, 4))
    live_frame.pack(fill="x")
    live_var = tk.BooleanVar(value=False)
    live_chk = ttk.Checkbutton(live_frame, text="常驻监听（不用按键）",
                               variable=live_var)
    live_chk.pack(side="left")
    live_state_var = tk.StringVar(value="未启动")
    ttk.Label(live_frame, textvariable=live_state_var,
              foreground="#0a7a4a").pack(side="left", padx=8)

    live = {"engine": None}

    def _live_cfg():
        import voice_engine as ve
        c = ve.merge_voice_defaults(dict(cfg))
        c["device"] = dev_var.get()
        c["stt_url"] = url_var.get().strip()
        # 常驻模式走 v0.3 /agent（带 Session 上下文）
        c["agent_url"] = c.get("agent_url") or ""
        c["tts_enabled"] = bool(speak_var.get())
        return c

    def _live_handle(pcm, meta):
        """常驻模式的一轮处理（在工作线程里跑）。"""
        import voice_engine as ve
        eng = live["engine"]
        if eng is None:
            return
        try:
            eng.set_state(ve.VoiceState.TRANSCRIBING)
            try:
                stt = transcribe(pcm_to_wav(pcm), url_var.get().strip())
            except Exception as exc:
                root.after(0, lambda e=exc: log("[常驻] STT 失败：%s" % e))
                return
            text = (stt.get("text") or "").strip()
            root.after(0, lambda: log("[常驻] 📝 %s" % (text or "(空)")))
            if not text:
                return
            agent_url = (_live_cfg().get("agent_url") or "").strip()
            if not agent_url:
                return
            eng.set_state(ve.VoiceState.AGENT_RUNNING)
            try:
                res = send_agent(text, agent_url, cfg.get("session_id",
                                                          "living-room-current"))
            except Exception as exc:
                root.after(0, lambda e=exc: log("[常驻] Agent 失败：%s" % e))
                return
            reply = (res.get("reply") or "").strip()
            root.after(0, lambda: log("[常驻] 🤖 %s%s" % (
                res.get("intent"), ("　💬 " + reply) if reply else "")))
            if reply and speak_var.get():
                eng.pause_wake()
                eng.set_state(ve.VoiceState.RESPONDING)
                try:
                    play_audio_bytes(synthesize_tts(reply, cfg.get("tts_url", "")))
                except Exception as exc:
                    root.after(0, lambda e=exc: log("[常驻] TTS 失败：%s" % e))
        finally:
            eng.finish_turn()

    def start_live():
        import voice_engine as ve
        if live["engine"] is not None:
            return
        try:
            eng = ve.VoiceEngine(
                _live_cfg(), ffmpeg,
                on_state=lambda st, d="": root.after(
                    0, lambda: live_state_var.set(st.value)),
                on_utterance=lambda pcm, meta: threading.Thread(
                    target=_live_handle, args=(pcm, meta), daemon=True).start(),
                on_log=lambda m: root.after(0, lambda: log("[常驻] %s" % m)))
            eng.start()
        except Exception as exc:
            live_var.set(False)
            live_state_var.set("启动失败")
            log("[常驻] 启动失败：%s" % exc)
            return
        if eng.state == ve.VoiceState.ERROR or eng.wake is None:
            live_var.set(False)
            live_state_var.set("唤醒词不可用")
            log("[常驻] 唤醒词不可用：%s" % (eng.wake_error or "见日志"))
            log("[常驻] 安装：python -m pip install openwakeword "
                "-i https://pypi.tuna.tsinghua.edu.cn/simple")
            eng.stop()
            return
        live["engine"] = eng
        live_state_var.set("等待唤醒")
        log("[常驻] 已启动，喊「%s」后直接说话" % cfg.get("voice", {})
            .get("wake_word", "hey_jarvis"))

    def stop_live():
        eng = live["engine"]
        live["engine"] = None
        if eng is not None:
            eng.stop()
        live_state_var.set("未启动")
        log("[常驻] 已停止")

    def toggle_live():
        if live_var.get():
            start_live()
        else:
            stop_live()

    live_chk.configure(command=toggle_live)

    # --- PTT 大按钮
    btn_frame = ttk.Frame(root, padding=(12, 4, 12, 4))
    btn_frame.pack(fill="x")
    ptt = tk.Button(
        btn_frame, text="按住说话\n（松开即识别）", font=("Microsoft YaHei UI", 14),
        height=4, relief="raised", bg="#e8eef7", activebackground="#c9dcf5",
    )
    ptt.pack(fill="x")

    hint_var = tk.StringVar(value="")
    ttk.Label(root, textvariable=hint_var, padding=(12, 0, 12, 4),
              foreground="#666").pack(fill="x")

    # --- 结果
    result_frame = ttk.Frame(root, padding=(12, 4, 12, 10))
    result_frame.pack(fill="both", expand=True)
    ttk.Label(result_frame, text="识别结果").pack(anchor="w")
    text = tk.Text(result_frame, height=9, wrap="word", font=("Microsoft YaHei UI", 11))
    text.pack(fill="both", expand=True, side="left")
    sb = ttk.Scrollbar(result_frame, orient="vertical", command=text.yview)
    sb.pack(side="right", fill="y")
    text.configure(yscrollcommand=sb.set)

    def log(line: str) -> None:
        text.insert("end", line + "\n")
        text.see("end")

    # --- 录音流程（在后台线程，避免阻塞 UI）

    def start_recording(*_):
        if state["recording"]:
            return
        device = dev_var.get()
        if not device:
            status_var.set("未选麦克风")
            return
        try:
            rec = Recorder(ffmpeg, device, cfg.get("max_seconds", 30))
            rec.start()
        except Exception as exc:
            status_var.set("启动失败")
            log("[错误] 启动录音失败：%s" % exc)
            return
        state["recorder"] = rec
        state["recording"] = True
        status_var.set("启动麦克风…")
        ptt.configure(bg="#f7c1c1", activebackground="#f09595", text="启动中…")

        def wait_ready():
            ok = rec.ready.wait(timeout=3.0)

            def upd():
                if state["recording"]:
                    ptt.configure(text="● 录音中\n（松开结束）")
                    status_var.set("录音中…" if ok else "录音中（设备启动慢）")

            root.after(0, upd)

        threading.Thread(target=wait_ready, daemon=True).start()

    def stop_recording(*_):
        if not state["recording"]:
            return
        state["recording"] = False
        rec = state["recorder"]
        state["recorder"] = None
        ptt.configure(bg="#e8eef7", activebackground="#c9dcf5",
                      text="按住说话\n（松开即识别）")
        status_var.set("处理中…")

        def work():
            pcm, dur, err = rec.stop()
            state["last_pcm"], state["last_duration"] = pcm, dur
            if not pcm:
                root.after(0, lambda: (status_var.set("无音频"),
                                       log("[错误] 没录到音频。ffmpeg: %s" % (err or "无输出"))))
                return
            peak = pcm_peak_dbfs(pcm)
            wav = pcm_to_wav(pcm)
            t0 = time.time()
            try:
                res = transcribe(wav, url_var.get())
            except Exception as exc:
                root.after(0, lambda e=exc: (status_var.set("识别失败"),
                                             log("[错误] %s" % e)))
                return
            total = time.time() - t0
            txt = (res.get("text") or "").strip()

            # Phase 4/5：把文本交给 Voice Gateway，由它经 HA 编排音乐动作。
            cmd_res = {}
            cmd_err = ""
            if txt:
                try:
                    cmd_res = send_command(txt, gw_var.get().strip(),
                                           speak=speak_var.get())
                except Exception as exc:
                    cmd_err = str(exc)[:160]

            # Phase 5：播放 gateway 合成的播报语音（在线程里，避免卡 UI）
            if cmd_res.get("audio_b64"):
                err = play_audio_b64(cmd_res["audio_b64"],
                                     cmd_res.get("audio_mime", "audio/mpeg"))
                if err:
                    cmd_res["tts_play_error"] = err

            def show():
                status_var.set("就绪")
                log("[%.2fs 音频 / 峰值 %.1f dBFS] %s" % (dur, peak, txt or "(空)"))
                if peak < -50:
                    log("   提示：电平极低，麦克风可能没开或没配对")
                log("   服务端 %.2fs（音频 %.2fs，RTF %s）｜ 端到端 %.2fs" % (
                    res.get("elapsed_seconds", -1), res.get("audio_seconds", -1),
                    res.get("rtf", "?"), total))
                if cmd_err:
                    log("   [指令] 下发失败：%s" % cmd_err)
                elif cmd_res:
                    path = cmd_res.get("path", "rules")
                    tag = "LLM" if path == "agent" else "规则"
                    line = "   [指令·%s] %s → %s（%.2fs）" % (
                        tag, cmd_res.get("intent"), cmd_res.get("script") or "无需下发",
                        cmd_res.get("gateway_seconds", -1))
                    if cmd_res.get("agent_args"):
                        line += " args=%s" % json.dumps(
                            cmd_res["agent_args"], ensure_ascii=False)
                    if not cmd_res.get("ok", True):
                        line += " ⚠️HA 返回 %s" % cmd_res.get("status")
                    log(line)
                    if cmd_res.get("say"):
                        log("   [播报] %s" % cmd_res["say"])
                    if cmd_res.get("agent_fallback"):
                        log("   [降级] LLM 未产出动作（%s），已回退规则"
                            % cmd_res["agent_fallback"])
                    if cmd_res.get("tts_play_error"):
                        log("   [TTS] 播放失败：%s" % cmd_res["tts_play_error"])

            root.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    ptt.bind("<ButtonPress-1>", start_recording)
    ptt.bind("<ButtonRelease-1>", stop_recording)

    # --- 文件上传（无需说话即可验证链路）
    def pick_file():
        path = filedialog.askopenfilename(
            title="选择 WAV 文件", filetypes=[("WAV", "*.wav"), ("所有文件", "*.*")])
        if not path:
            return
        status_var.set("处理中…")

        def work():
            with open(path, "rb") as fh:
                data = fh.read()
            try:
                res = transcribe(data, url_var.get())
            except Exception as exc:
                root.after(0, lambda e=exc: (status_var.set("识别失败"),
                                             log("[错误] %s" % e)))
                return
            root.after(0, lambda: (status_var.set("就绪"),
                                   log("[文件 %s] %s" % (os.path.basename(path),
                                                         res.get("text", "")))))

        threading.Thread(target=work, daemon=True).start()

    bottom = ttk.Frame(root, padding=(12, 0, 12, 10))
    bottom.pack(fill="x")
    ttk.Button(bottom, text="上传 WAV 测试", command=pick_file).pack(side="left")
    ttk.Button(bottom, text="清空", command=lambda: text.delete("1.0", "end")).pack(side="left", padx=6)

    def save_cfg():
        cfg["stt_url"] = url_var.get().strip()
        cfg["gateway_url"] = gw_var.get().strip()
        cfg["device"] = dev_var.get()
        cfg["tts_enabled"] = bool(speak_var.get())
        try:
            save_config(cfg)
            status_var.set("已保存")
        except Exception as exc:
            messagebox.showerror("错误", "保存失败：%s" % exc)

    ttk.Button(bottom, text="保存配置", command=save_cfg).pack(side="right")

    # --- 全局热键（可选）
    hotkey_ok = False
    if cfg.get("hotkey_enabled", True):
        try:
            import keyboard  # type: ignore

            keyboard.on_press_key("f9", lambda _e: root.after(0, start_recording))
            keyboard.on_release_key("f9", lambda _e: root.after(0, stop_recording))
            hotkey_ok = True
        except Exception:
            hotkey_ok = False
    hint_var.set("按住按钮说话；按 F9 也可（按住说、松开停）" if hotkey_ok
                 else "按住按钮说话（装 keyboard 库后可用 F9 热键）")

    refresh_devices(announce=False)
    log("STT 目标：%s" % url_var.get())
    log("已发现 %d 个录音设备。" % len(state["devices"]))
    log("提示：点「上传 WAV 测试」可先验证链路，不必说话。")
    root.mainloop()
    return 0


# ---------------------------------------------------------------- CLI


def cli_list_devices() -> int:
    ffmpeg = find_ffmpeg()
    devs = list_audio_devices(ffmpeg)
    print("ffmpeg: %s" % ffmpeg)
    for i, d in enumerate(devs):
        print("  [%d] %s" % (i, d))
    return 0


def cli_record(device: str, seconds: float, stt_url: str, gateway_url: str = "") -> int:
    ffmpeg = find_ffmpeg()
    print("录音设备：%s" % device)
    rec = Recorder(ffmpeg, device, max_seconds=int(seconds) + 5)
    rec.start()
    time.sleep(seconds)
    pcm, dur, err = rec.stop()
    if not pcm:
        print("没录到音频。ffmpeg 输出：%s" % (err or "无"))
        return 2
    print("录到 %.2fs，峰值 %.1f dBFS" % (dur, pcm_peak_dbfs(pcm)))
    if err:
        print("ffmpeg stderr: %s" % err[:500])
    t0 = time.time()
    res = transcribe(pcm_to_wav(pcm), stt_url)
    e2e = time.time() - t0
    print(json.dumps(res, ensure_ascii=False, indent=2))
    txt = (res.get("text") or "").strip()
    print("端到端（松开→文本）: %.2fs" % e2e)
    if txt and gateway_url:
        try:
            cmd = send_command(txt, gateway_url)
            print("指令下发: %s" % json.dumps(cmd, ensure_ascii=False))
        except Exception as exc:
            print("指令下发失败: %s" % exc)
    return 0


def cli_mic_level(device: str, seconds: float) -> int:
    """只测麦克风电平：不送 STT、不下发指令。

    用途是验收前的「麦克风链路是否真的通」判定。实测参考值（2026-09-13，DJI Mic）：
      - 数字静音（TX 未与 RX 配对）  ≈ -91 dBFS  ← 会被误当成「设备没插」
      - 环境噪声                     ≈ -61 dBFS
      - 正常说话                     ≈ -32 dBFS
    """
    ffmpeg = find_ffmpeg()
    print("录音设备：%s" % device)
    print("采集 %.1f 秒 —— 建议先说 3 秒静音、再说 3 秒话，好对比底噪与人声。" % seconds)
    rec = Recorder(ffmpeg, device, max_seconds=int(seconds) + 5)
    rec.start()
    time.sleep(seconds)
    pcm, dur, err = rec.stop()
    if not pcm:
        print("没录到音频。ffmpeg 输出：%s" % (err or "无"))
        return 2
    peak = pcm_peak_dbfs(pcm)
    print("录到 %.2fs，峰值 %.1f dBFS" % (dur, peak))
    if peak < -80:
        print("[FAIL] 判定：数字静音 —— 麦克风没在工作。检查 DJI TX 是否与接收器配对，"
              "或改用蓝牙 HFP 通道。")
        return 3
    if peak < -55:
        print("[!] 判定：电平偏低 —— 能录到声音但很轻，唤醒词可能不稳。")
    else:
        print("[OK] 判定：电平正常（说话段应在 -40 ~ -20 dBFS 之间）。")
    if err:
        print("ffmpeg stderr: %s" % err[:300])
    return 0


def cli_file(path: str, stt_url: str) -> int:
    with open(path, "rb") as fh:
        data = fh.read()
    res = transcribe(data, stt_url)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    # ⚠️ 不要改成 reconfigure(encoding="utf-8")：PowerShell 控制台是 CP936，
    #    强推 UTF-8 字节会让中文全变乱码（2026-09-13 实测）。详见 _console.py。
    _console.setup()

    cfg = load_config()
    ap = argparse.ArgumentParser(description="Home Music Agent — Windows Voice Client")
    ap.add_argument("--list-devices", action="store_true", help="列出录音设备后退出")
    ap.add_argument("--record", type=float, metavar="SECONDS", help="录 N 秒并识别（无界面）")
    ap.add_argument("--mic-level", type=float, metavar="SECONDS",
                    help="只测麦克风电平（不识别、不下发），验收前自检用")
    ap.add_argument("--device", default=cfg.get("device") or "", help="录音设备名")
    ap.add_argument("--file", help="直接上传一个 wav 文件识别")
    ap.add_argument("--stt-url", default=cfg["stt_url"], help="STT 服务地址")
    ap.add_argument("--gateway-url", default=cfg.get("gateway_url", ""),
                    help="Voice Gateway 地址；给了就在转写后下发音乐指令")
    ap.add_argument("--say", help="跳过滤音，直接把这段文本作为转写结果下发（联调用）")
    ap.add_argument("--agent-url", default=cfg.get("agent_url", ""),
                    help="v0.3 Agent 入口（/agent）；--say 时优先用它")
    ap.add_argument("--voice", action="store_true",
                    help="常驻语音模式：喊唤醒词后直接说话，无需按键（v0.3）")
    args = ap.parse_args()

    def pick_device(explicit: str) -> str:
        """未显式指定设备时，优先挑 DJI / Wireless 相关设备。"""
        if explicit:
            return explicit
        devs = list_audio_devices()
        return next((d for d in devs if "DJI" in d or "Wireless" in d),
                    devs[0] if devs else "")

    if args.say:
        # 优先走 v0.3 /agent（带 Session 上下文），没配才退回旧 /command。
        url = (args.agent_url or "").strip() or args.gateway_url
        if url.rstrip("/").endswith("/agent"):
            res = send_agent(args.say, url,
                             cfg.get("session_id", "living-room-current"))
        else:
            res = send_command(args.say, url)
        print("指令下发: %s" % json.dumps(res, ensure_ascii=False))
        return 0
    if args.voice:
        return run_voice_loop(cfg)
    if args.list_devices:
        return cli_list_devices()
    if args.file:
        return cli_file(args.file, args.stt_url)

    if args.mic_level or args.record:
        device = pick_device(args.device)
        if not device:
            print("没有可用录音设备")
            return 2
        if args.mic_level:
            return cli_mic_level(device, args.mic_level)
        return cli_record(device, args.record, args.stt_url, args.gateway_url)
    return run_gui(cfg)


if __name__ == "__main__":
    sys.exit(main())
