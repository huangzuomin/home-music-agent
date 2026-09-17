#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-03b — PlaybackState：播放器真实状态的带时效快照。

原则（ADR-01）：
    * MA 是真实状态的唯一事实来源；快照只是带 freshness 的缓存；
    * 显式 player_id，经 get_active_queue 解析活动 queue_id
      （部署版 queue_id == player_id，但不默认相等——代码按返回值解析）；
    * 绑定播放器离线 → 如实呈现 offline，**不回退**到其他播放器（T09/T22）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_FRESHNESS_SEC = 5.0


@dataclass
class PlaybackSnapshot:
    player_id: str
    player_name: str
    playback_state: str                 # playing / paused / idle / offline
    available: bool
    volume_level: float | None
    current_item: dict[str, Any]        # {uri, title, artist, duration}
    elapsed_time: float
    queue_id: str
    queue_length: int
    next_item: dict[str, Any]
    state_updated_at: float             # 单调时钟（freshness 判定）
    fresh: bool = True

    def is_stale(self, now: float | None = None,
                 max_age: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        age = now - self.state_updated_at
        return age > (max_age if max_age is not None else DEFAULT_FRESHNESS_SEC)


def _empty(player_id: str, reason: str) -> PlaybackSnapshot:
    return PlaybackSnapshot(
        player_id=player_id, player_name="", playback_state=reason,
        available=False, volume_level=None, current_item={}, elapsed_time=0.0,
        queue_id="", queue_length=0, next_item={}, state_updated_at=time.monotonic(),
        fresh=True)


def build_snapshot(reader, player_id: str,
                   freshness_sec: float = DEFAULT_FRESHNESS_SEC) -> PlaybackSnapshot:
    """从 MAReader（或兼容对象）构建指定播放器的真实状态快照。

    reader 需提供：get_player(player_id)、get_active_queue(player_id)、
    queue_items(queue_id, limit)。离线/不存在 → state="offline"。
    """
    t0 = time.monotonic()
    player = reader.get_player(player_id)
    if player is None:
        snap = _empty(player_id, "offline")
        snap.state_updated_at = t0
        return snap

    queue = reader.get_active_queue(player_id) or {}
    qid = str(queue.get("queue_id") or "")
    current = queue.get("current_item") or {}
    media = current.get("media_item") or current
    next_item = queue.get("next_item") or {}

    items = None
    try:
        items = reader.queue_items(qid, limit=1)
    except Exception:
        pass

    def brief(itm: dict[str, Any]) -> dict[str, Any]:
        media_i = itm.get("media_item") or itm
        names = [(a.get("name") or "").strip()
                 for a in (media_i.get("artists") or [])]
        return {"uri": media_i.get("uri") or itm.get("uri") or "",
                "title": media_i.get("name") or itm.get("name") or "",
                "artist": " / ".join(n for n in names if n),
                "duration": media_i.get("duration") or itm.get("duration") or 0}

    snap = PlaybackSnapshot(
        player_id=player_id,
        player_name=str(player.get("name") or ""),
        playback_state=str(player.get("state") or "unknown"),
        available=bool(player.get("available", True)),
        volume_level=player.get("volume_level"),
        current_item=brief(current) if current else {},
        elapsed_time=float(queue.get("elapsed_time") or 0.0),
        queue_id=qid,
        queue_length=int(queue.get("items") or 0),
        next_item=brief(next_item) if next_item else {},
        state_updated_at=t0,
        fresh=True,
    )
    return snap
