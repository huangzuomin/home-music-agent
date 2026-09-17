#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 openWakeWord 是否真的可用（装完依赖后跑一次）。

四步：import → 加载 hey_jarvis → 喂静音看分数 → 走一遍 voice_engine.WakeWord 封装。

首次加载会从 GitHub 下载模型（约 2MB）。若下载失败，手动放置到：
    <site-packages>/openwakeword/resources/models/hey_jarvis_v0.1.onnx
下载地址见 openWakeWord 仓库的 releases。

用法：python check_wakeword.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _console  # noqa: E402


def main() -> int:
    _console.setup()
    print("① import openwakeword")
    try:
        import openwakeword
        print("   [OK] 版本 %s" % getattr(openwakeword, "__version__", "未知"))
    except Exception as exc:
        print("   [FAIL] %s: %s" % (type(exc).__name__, exc))
        print("   安装：python -m pip install openwakeword "
              "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    print("② 加载 hey_jarvis 模型（首次会联网下载）")
    try:
        from openwakeword.model import Model
        model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        print("   [OK] 已加载")
    except Exception as exc:
        print("   [FAIL] %s: %s" % (type(exc).__name__, exc))
        print("   多为模型下载失败。请手动下载 hey_jarvis_v0.1.onnx 放到：")
        try:
            import openwakeword as ow
            print("   %s/resources/models/" % os.path.dirname(ow.__file__))
        except Exception:
            print("   <site-packages>/openwakeword/resources/models/")
        return 1

    print("③ 喂 1280 采样静音，看分数（应接近 0）")
    try:
        import numpy as np
        preds = model.predict(np.zeros(1280, dtype=np.int16))
        print("   [OK] %s" % {k: round(float(v), 4) for k, v in preds.items()})
    except Exception as exc:
        print("   [FAIL] %s: %s" % (type(exc).__name__, exc))
        return 1

    print("④ 走 voice_engine.WakeWord 封装（4 帧 20ms = 80ms）")
    try:
        from voice_engine import WakeWord
        w = WakeWord("hey_jarvis", 0.6)
        score = 0.0
        for _ in range(4):
            score = w.feed(b"\x00" * 640)
        print("   [OK] 静音分数 = %.4f" % score)
    except Exception as exc:
        print("   [FAIL] %s: %s" % (type(exc).__name__, exc))
        return 1

    print()
    print("[OK] 唤醒词就绪。现在可以跑：run.bat --voice")
    print("   然后对着麦克风说「Hey Jarvis」，再说音乐指令。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
