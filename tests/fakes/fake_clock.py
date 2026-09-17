"""FakeClock：可推进的时钟，用于暂停时长 / 会话 TTL / 定时结束等语义。

通过 monkeypatch 替换被测模块的 ``time`` 名（模块级可替换依赖），
只影响该模块，不污染进程全局时钟。
"""
from __future__ import annotations

import time as _realtime


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = float(start)

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)

    # ---- 被测模块用到的 time API ----
    def time(self) -> float:
        return self.t

    def strftime(self, fmt, t=None):
        ts = self.t if t is None else (t if isinstance(t, (int, float)) else None)
        return _realtime.strftime(fmt, _realtime.localtime(ts))

    def localtime(self, t=None):
        return _realtime.localtime(self.t if t is None else t)

    def mktime(self, tt):
        return _realtime.mktime(tt)
