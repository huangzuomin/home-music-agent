#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 控制台输出编码适配（voice-client 内部工具共用）。

⚠️ 2026-09-13 实测：PowerShell 默认活动代码页 936（GBK），两种常见写法都会翻车 ——

  1) ``sys.stdout.reconfigure(encoding="utf-8")``
     Python 吐 UTF-8 字节，控制台按 GBK 解码 → **中文全变乱码**
     实测：「列出录音设备后退出」显示成「鍒楀嚭褰曢煶璁惧鍚庨€€鍑」

  2) 什么都不做
     中文正常，但 ✅ ❌ ⚠️ 这类 emoji 不在 GBK 字符集里 →
     ``UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'`` → **直接崩溃**

正确做法 = **沿用控制台自身的编码**，只把错误处理放宽为 ``replace``：

  - 中文永远正确（GBK 完整覆盖简体中文）
  - emoji 在 GBK 控制台下降级成 ``?``，而不是让脚本崩掉
  - 若控制台本身就是 UTF-8（``chcp 65001``），emoji 也能正常显示

``run.bat`` 已内置 ``chcp 65001``，所以走正常入口时 emoji 是能显示的；
直接调 python 时则优雅降级。
"""
from __future__ import annotations

import sys


def setup() -> None:
    """把 stdout/stderr 调整为「控制台原生编码 + 错误替换」。幂等，可重复调用。

    必须在任何 print 之前调用。
    """
    try:
        enc = sys.stdout.encoding or "utf-8"
    except Exception:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass
