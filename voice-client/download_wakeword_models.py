#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 openWakeWord 预训练模型（hey_jarvis）。

国内直连 GitHub 可能失败；失败时脚本会打印目标目录，可手动下载后放入。

用法：python download_wakeword_models.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _console  # noqa: E402

MODELS = ["hey_jarvis"]


def target_dir() -> str:
    try:
        import openwakeword
        return os.path.join(os.path.dirname(openwakeword.__file__),
                            "resources", "models")
    except Exception:
        return "<site-packages>/openwakeword/resources/models"


def main() -> int:
    _console.setup()
    print("目标目录：%s" % target_dir())
    print("下载模型：%s" % ", ".join(MODELS))
    try:
        from openwakeword.utils import download_models
    except Exception as exc:
        print("[FAIL] 无法 import openwakeword：%s: %s" % (type(exc).__name__, exc))
        return 1
    try:
        download_models(model_names=MODELS)
    except Exception as exc:
        print("[FAIL] 下载失败：%s: %s" % (type(exc).__name__, exc))
        print()
        print("手动方案：从 openWakeWord 的 GitHub releases 下载")
        print("  hey_jarvis_v0.1.onnx")
        print("放到：%s" % target_dir())
        return 1

    d = target_dir()
    got = []
    if os.path.isdir(d):
        got = sorted(f for f in os.listdir(d) if f.endswith((".onnx", ".tflite")))
    print("[OK] 下载完成，现有模型文件：")
    for f in got:
        p = os.path.join(d, f)
        print("   %-42s %8.1f KB" % (f, os.path.getsize(p) / 1024.0))
    if not any("jarvis" in f for f in got):
        print("[!] 没看到 jarvis 模型，请检查上面的目录")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
