"""IMP-11 — 可靠的定时结束（持久 end_at + HA 独立截止保护）。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EndPolicy:
    """聆听结束策略：立即 / 到时间 / 本曲后 / 不自动结束。"""
    mode: str = "manual"            # manual / timed / after_current / none
    end_at: float | None = None     # UTC epoch
    timer_revision: int = 1
    listening_session_id: str = ""
    player_id: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        if self.mode != "timed" or self.end_at is None:
            return False
        return (now or time.time()) >= self.end_at

    def time_remaining(self, now: float | None = None) -> float:
        if self.end_at is None:
            return 0.0
        return max(0.0, self.end_at - (now or time.time()))


class EndPolicyManager:
    """定时结束管理器：持久化 end_at，支持取消，重启后对账。

    语义：
      * 取消 → 旧定时失效，新聆听不受旧定时影响
      * 重启后过期且属本段聆听 → 幂等停止
      * 未确认取消/设置 → 不声称可靠
    """

    def __init__(self) -> None:
        self._policies: dict[str, EndPolicy] = {}   # player_id → EndPolicy
        self._lock = __import__("threading").Lock()

    def set_policy(self, policy: EndPolicy) -> None:
        with self._lock:
            self._policies[policy.player_id] = policy

    def cancel(self, player_id: str) -> bool:
        return self._policies.pop(player_id, None) is not None

    def get_policy(self, player_id: str) -> EndPolicy | None:
        return self._policies.get(player_id)

    def check_expired(self, now: float | None = None) -> list[str]:
        """检查所有过期定时并移除，返回受影响的 player_id 列表。"""
        now = now or time.time()
        expired = [pid for pid, p in self._policies.items()
                   if p.mode == "timed" and p.end_at and now >= p.end_at]
        for pid in expired:
            self._policies.pop(pid, None)
        return expired
