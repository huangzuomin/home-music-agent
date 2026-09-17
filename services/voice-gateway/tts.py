#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 5 — TTS 播报（微软 Edge TTS，免费、无需 API Key）。

策略：
    * 生成到内存（bytes，mp3），不落盘，直接返回给调用方。
    * 优先本地播报：容器内没有声卡，所以实际由「客户端拉取」或「HA 播放」二选一。
      本模块只负责**合成**，播放交给上层决定（见 app.py 的 /tts 接口）。

可用中文音色（实测 VM1 直连可获取 8 个）：
    zh-CN-XiaoxiaoNeural  女声，自然（默认）
    zh-CN-XiaoyiNeural    女声，年轻
    zh-CN-YunxiNeural     男声，青年
    zh-CN-YunjianNeural   男声，沉稳
"""
from __future__ import annotations

import io
from typing import Any

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 这些播报语太短太机械，交给 Edge TTS 直接读反而更自然，
# 不需要再过一遍 LLM（省一次调用、省 1-2 秒延迟）。
VOICE_CHOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
]


class TTSError(RuntimeError):
    """语音合成失败。"""


async def synthesize(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """把文本合成为 mp3 字节流。调用方需在 async 上下文里 await。"""
    text = (text or "").strip()
    if not text:
        raise TTSError("empty text")
    if voice not in VOICE_CHOICES:
        voice = DEFAULT_VOICE

    try:
        import edge_tts
    except ImportError as e:  # pragma: no cover
        raise TTSError("edge-tts not installed: %s" % e) from None

    buf = io.BytesIO()
    try:
        communicate = edge_tts.Communicate(text, voice)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buf.write(chunk["data"])
    except Exception as e:  # noqa: BLE001
        raise TTSError("%s: %s" % (type(e).__name__, e)) from None

    data = buf.getvalue()
    if not data:
        raise TTSError("no audio produced")
    return data


async def list_voices(locale_prefix: str = "zh-CN") -> list[dict[str, Any]]:
    """列出可用音色（用于 /tts/voices 调试接口）。"""
    try:
        import edge_tts
        voices = await edge_tts.list_voices()
    except Exception as e:  # noqa: BLE001
        raise TTSError("%s: %s" % (type(e).__name__, e)) from None
    return [
        {"name": v["ShortName"], "gender": v.get("Gender"), "locale": v.get("Locale")}
        for v in voices
        if str(v.get("Locale", "")).startswith(locale_prefix)
    ]
