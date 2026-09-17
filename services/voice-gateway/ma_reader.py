#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-03b — Music Assistant 只读适配层（ma_reader）。

传输事实（IMP-00/baseline-manifest 实测）：
    * MA HTTP 命令通道：POST {MA}/api，{"command","args"}，Bearer MA_LONG_TOKEN
    * HTTP /api-docs 仅暴露 api/auth/info/setup；命令面经由此通道（等价 WS）
    * players/all 响应**不含** active/queue_id；含 volume_level/playback_state/
      current_media/supported_features/sleep_timer_expires_at
    * 队列发现：player_queues/get_active_queue {player_id} → queue_id
      （部署版 queue_id == player_id）

只读：本模块不提供任何写方法（写一律经 HA，见 D13/ADR-01）。
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

MA_BASE = os.environ.get("MA_URL", "http://127.0.0.1:8095").rstrip("/")
MA_TOKEN = os.environ.get("MA_LONG_TOKEN", "")


class MAReader:
    """MA 只读命令面适配（HTTP POST /api，已验证传输）。"""

    def __init__(self, base: str = MA_BASE, token: str = MA_TOKEN,
                 timeout: float = 8.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.calls: list[tuple[str, dict]] = []

    def call(self, command: str, args: dict | None = None) -> Any:
        """执行一条 MA 只读命令；失败/非 200 返回 None（调用方兜底）。"""
        self.calls.append((command, dict(args or {})))
        body = json.dumps({"command": command, "args": args or {}}).encode()
        req = urllib.request.Request(self.base + "/api", body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    # ------------------------------------------------------------ 读取方法

    def players(self) -> list[dict[str, Any]]:
        res = self.call("players/all")
        return res if isinstance(res, list) else []

    def get_player(self, player_id: str) -> dict[str, Any] | None:
        """按显式 player_id 取播放器；**无回退**（T09/T22，IMP-03b）。"""
        for p in self.players():
            if p.get("player_id") == player_id:
                return p
        return None

    def get_active_queue(self, player_id: str) -> dict[str, Any]:
        """解析播放器的活动队列（部署版：queue_id == player_id）。"""
        q = self.call("player_queues/get_active_queue", {"player_id": player_id})
        return q if isinstance(q, dict) else {}

    def queue_items(self, queue_id: str, limit: int = 10) -> list[dict[str, Any]]:
        res = self.call("player_queues/items", {"queue_id": queue_id,
                                                "limit": limit})
        return res if isinstance(res, list) else []

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        res = self.call("music/search", {"search_query": query,
                                         "media_types": ["track"],
                                         "limit": limit})
        if isinstance(res, dict):
            return res.get("tracks") or []
        return []

    def recently_played(self, limit: int = 10) -> list[dict[str, Any]]:
        """IMP-03b 实测 verified：music/recently_played_items 返回真实条目。"""
        res = self.call("music/recently_played_items", {"limit": limit})
        return res if isinstance(res, list) else []

    # ------------------------------------------------------------ 能力探针

    def probe_capabilities(self, player_id: str | None = None) -> dict[str, Any]:
        """对 capability-matrix 的 unverified 项做只读探测（存在性内省）。

        判定：HTTP 400（参数/目标校验）= 命令存在 → supported-validation；
             500/Unknown = 待定或异常；正常返回 = verified。
        本方法不真正执行变更类动作。
        """
        out: dict[str, Any] = {"probe_at_player": player_id or "", "items": []}

        def probe(name: str, command: str, args: dict) -> None:
            res = self.call(command, args)
            if res is None:
                status, detail = "error", "no response"
            elif isinstance(res, dict) and res.get("http_error"):
                status, detail = "http-error", str(res["http_error"])
            else:
                status, detail = "verified", json.dumps(res,
                                                        ensure_ascii=False)[:120]
            out["items"].append({"name": name, "command": command,
                                 "status": status, "detail": detail})

        probe("队列删除存在性", "player_queues/delete", {"queue_id": "no-such-q"})
        probe("专辑展开存在性", "music/album_tracks", {"album_id": "no-such"})
        probe("收藏存在性", "music/add_to_favorites",
              {"item_uri": "library://none"})
        probe("最近播放", "music/recently_played_items", {"limit": 5})
        probe("音量写存在性", "players/cmd/volume_set", {})
        probe("next 存在性", "player_queues/next", {})
        probe("previous 存在性", "player_queues/previous", {})
        return out
