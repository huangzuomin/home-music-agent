"""FakeMA：Music Assistant 命令面的离线替身。

响应形状以 docs/implementation/baseline-manifest.json 与
capability-matrix.md 的**实测**为准：
    * 命令通道 = POST /api/call {"command", "args"}
    * players/all 的条目**没有** active / queue_id 字段
    * 播放器/队列/搜索不在 OpenAPI（HTTP 面仅 api/auth/info/setup）
可注入：失败（fail_next）、外部换歌（set_current）、离线（available=False）。
"""
from __future__ import annotations


class FakeMA:
    def __init__(self) -> None:
        # ⚠️ 刻意不含 active / queue_id 字段（与部署版实测一致）
        self.players: list[dict] = [
            {"player_id": "sq-1", "name": "Squeezebox Touch",
             "state": "playing", "available": True, "type": "squeezebox",
             "volume_level": 88.0},
            {"player_id": "cast-1", "name": "study-cast",
             "state": "idle", "available": True, "type": "cast",
             "volume_level": 50.0},
        ]
        # player_id -> 队列状态
        self.queues: dict[str, dict] = {
            "sq-1": {
                "queue_id": "q-sq-1",
                "state": "playing",
                "elapsed_time": 42.0,
                "current_index": 0,
                "items": 2,
                "current_item": {
                    "uri": "library://track/31",
                    "name": "晴天",
                    "artist": "周杰伦",
                    "duration": 269.3,
                },
            },
        }
        self.library: list[dict] = [          # library:// 曲目（music/search 命中）
            {"uri": "library://track/31", "name": "晴天", "is_playable": True},
            {"uri": "library://track/41", "name": "十年", "is_playable": True},
        ]
        self.online: list[dict] = [           # 非 library://（在线垫场）
            {"uri": "qqmusic://track/999", "name": "晴天 (Live)", "is_playable": True},
        ]
        self.calls: list[tuple[str, dict]] = []
        self.fail_next = 0                    # >0 时接下来 N 次调用返回 None

    # ---- MAClient.api 兼容入口 ----
    def dispatch(self, command: str, args: dict | None = None):
        args = args or {}
        self.calls.append((command, args))
        if self.fail_next > 0:
            self.fail_next -= 1
            return None
        if command == "players/all":
            # 实测形状：不含 active / queue_id
            return [{k: v for k, v in p.items()
                     if k not in ("active", "queue_id")}
                    for p in self.players]
        if command == "player_queues/get_active_queue":
            return dict(self.queues.get(args.get("player_id") or "", {})
                        ) if args.get("player_id") in self.queues else {"state": "idle"}
        if command == "player_queues/items":
            q = self.queues.get(args.get("queue_id") or "")
            return (q or {}).get("items_list", [])[:args.get("limit", 10)]
        if command == "music/search":
            q = str(args.get("search_query") or "")
            hits = [t for t in (self.library + self.online) if q in t["name"]]
            return {"tracks": hits[:args.get("limit", 5)]}
        return None

    def queue_state(self, player_id: str) -> dict:
        return dict(self.queues.get(player_id) or {})

    def queue_items(self, queue_id: str, limit: int = 10) -> list[dict]:
        q = self.queues.get(queue_id) or {}
        return (q.get("items_list") or [])[:limit]

    # ---- MAClient.search 兼容入口 ----
    def search(self, query: str, limit: int = 5) -> list[dict]:
        res = self.dispatch("music/search",
                            {"search_query": query, "limit": limit})
        return res.get("tracks", []) if isinstance(res, dict) else []

    # ---- MAClient.pick_player 兼容入口 ----
    def pick_player(self) -> dict:
        for p in self.players:
            if p.get("available") and "squeezebox" in str(p.get("type", "")).lower():
                return p
        for p in self.players:
            if p.get("available"):
                return p
        return {}

    # ---- 场景注入 ----
    def set_player_offline(self, player_id: str) -> None:
        for p in self.players:
            if p["player_id"] == player_id:
                p["available"] = False

    def set_current(self, player_id: str, uri: str, title: str) -> None:
        """外部换歌（模拟物理遥控/MA UI 的人工操作）。"""
        self.queues.setdefault(player_id, {})["current_item"] = {
            "uri": uri, "name": title}
