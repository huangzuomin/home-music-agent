#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-10 — 聆听生命周期与队列策略（ListeningSession + 约束过滤）。

生命周期分离（计划 §4.1/§4.3）：
    ConversationSession  对话上下文（30min 空闲过期，不影响聆听约束）
    ListeningSession     聆听段（active/paused/ended/suspended）
约束规则：
    * 硬过滤先行：无人声 unknown 不得通过 no_vocal 硬约束
    * 软排序在后：偏好分数只影响顺序不影响通过
    * 排除规则统一作用于选曲/补位/续播
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any


class ListenStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"
    SUSPENDED = "suspended"


class ListeningSession:
    """一段聆听（区别于对话 Session）。

    生命周期：active → paused → active → ended
    外部人工改动无法归因 → suspended（尊重人工控制，不自动抢回）。
    """

    def __init__(self, session_id: str, player_id: str,
                 scene: str = "", constraints: dict | None = None) -> None:
        self.listening_session_id = session_id or uuid.uuid4().hex
        self.player_id = player_id
        self.scene = scene
        self.constraints = constraints or {}
        self.status = ListenStatus.ACTIVE
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.end_reason = ""
        self.epoch = 1

    def pause(self) -> None:
        if self.status == ListenStatus.ACTIVE:
            self.status = ListenStatus.PAUSED

    def resume(self) -> None:
        if self.status == ListenStatus.PAUSED:
            self.status = ListenStatus.ACTIVE

    def end(self, reason: str = "user_stop") -> None:
        self.status = ListenStatus.ENDED
        self.end_reason = reason

    def suspend(self) -> None:
        """外部人工接管 → 暂停自动策略，尊重人工操作。"""
        self.status = ListenStatus.SUSPENDED

    def is_active(self) -> bool:
        return self.status == ListenStatus.ACTIVE


# ---------------------------------------------------------------- 队列约束

def hard_filter(candidates: list[dict],
                constraints: dict[str, Any]) -> list[dict]:
    """硬过滤：不满足约束的候选直接排除。

    无人声约束只接受 verified_no；unknown 不能当满足（计划原文）。
    """
    no_vocal = constraints.get("no_vocal", False)
    out = []
    for c in candidates:
        vocal = str(c.get("vocal") or "unknown")
        if no_vocal and vocal != "verified_no":
            continue
        out.append(c)
    return out


def soft_sort(candidates: list[dict],
              preferences: dict[str, float] | None = None) -> list[dict]:
    """软排序：偏好分数降序，无偏好分按原始顺序。"""
    if not preferences:
        return list(candidates)
    return sorted(candidates,
                  key=lambda c: -preferences.get(c.get("artist", ""), 0))


def filter_queue(candidates: list[dict],
                 constraints: dict[str, Any],
                 preferences: dict[str, float] | None = None) -> list[dict]:
    """硬过滤 + 软排序，返回可入队的候选列表。"""
    filtered = hard_filter(candidates, constraints)
    return soft_sort(filtered, preferences)
