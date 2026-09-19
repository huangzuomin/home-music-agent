#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 升级（v0.3）— Music Context 构建（需求文档 §11）。

Agent 每次推理前必须拿到当前音乐状态，不能把用户一句话裸发给模型。

读取路径的边界（Sprint 1 拍板，2026-09-13）：
    * 本模块允许**只读直连** Music Assistant API（queue/items、active_queue）——
      HA 里没有队列与曲目明细，只有 MA 有。
    * **写路径铁律不变（D13）**：所有音乐动作仍一律经 HA script 下发，
      本模块没有任何写方法，也请别加。

凭据：优先用 .env 里的 MA_LONG_TOKEN（长期 token）；没有再用用户名密码登录。
最近播放：MA 无 recently_played 接口，由 TrackHistory 自行观测 now_playing 变化。
"""
from __future__ import annotations

import json
import pathlib
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

MA_BASE = "http://127.0.0.1:8095"
HISTORY_PATH = pathlib.Path("/app/data/sessions/track-history.jsonl")


# ------------------------------------------------------------------ MA 只读客户端


class MAClient:
    """Music Assistant 只读 API 客户端（Bearer token，自动续期一次）。"""

    def __init__(self, env_loader: Callable[[], dict[str, str]],
                 base: str = MA_BASE, timeout: float = 8.0) -> None:
        self._env = env_loader
        self._base = base.rstrip("/")
        self._timeout = timeout
        self._token = ""
        self._token_at = 0.0
        self._player_cache: dict[str, Any] = {}
        self._player_at = 0.0

    def _get_token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._token and now - self._token_at < 3600:
            return self._token
        cfg = self._env()
        # 铁律（项目记忆 #7）：token 一律两个变量名都试
        tok = cfg.get("MA_TOKEN") or cfg.get("MA_LONG_TOKEN") or ""
        if tok:
            self._token, self._token_at = tok, now
            return tok
        # 兜底：用户名密码换短期 token
        user, pw = cfg.get("MA_USERNAME", ""), cfg.get("MA_PASSWORD", "")
        if not user:
            return ""
        try:
            res = self._raw("/auth/login", {
                "provider_id": "builtin",
                "credentials": {"username": user, "password": pw},
            }, token="")
            tok = res.get("token") or res.get("access_token") or ""
            if tok:
                self._token, self._token_at = tok, now
        except Exception:
            pass
        return self._token

    def _raw(self, path: str, body: dict[str, Any] | None,
             token: str | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data,
                                     method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        if token is None:
            token = self._get_token()
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return json.loads(r.read().decode())

    def api(self, command: str, args: dict[str, Any] | None = None) -> Any:
        """调用 MA command；401 时强制刷新 token 重试一次。失败返回 None。"""
        body = {"command": command, "args": args or {}}
        for attempt in (0, 1):
            try:
                return self._raw("/api", body)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 0:
                    self._get_token(force=True)
                    continue
                return None
            except Exception:
                return None
        return None

    # ------------------------------------------------ 常用查询

    def pick_player(self) -> dict[str, Any]:
        """解析默认播放器（IMP-03b，T09/T22）。

        契约：只认默认的 squeezebox-touch；它离线/不存在时**如实返回其
        不可用状态**，不回退到其他播放器（读取、显示、写入必须是同一台）。
        其他播放器经 player_bindings 显式绑定后使用（IMP-04 配对/绑定）。
        缓存 30s。
        """
        now = time.time()
        if self._player_cache and now - self._player_at < 30:
            return self._player_cache
        players = self.api("players/all") or []
        if isinstance(players, dict):
            players = players.get("result") or []
        chosen: dict[str, Any] = {}
        for p in players:
            # MA 2.x 把所有播放器的 type 统一成 "player"，厂商信息挪到了
            # provider 字段（实测 squeezelite: type=player, provider=squeezelite）。
            # 所以匹配要横扫 type/provider/name 三处，squeezebox/squeezelite 都认。
            hay = " ".join(str(p.get(k) or "") for k in
                           ("type", "provider", "name")).lower()
            if "squeeze" in hay:
                chosen = p          # 命中默认绑定目标（available 与否都返回）
                break
        self._player_cache, self._player_at = chosen, now
        return chosen

    def queue_state(self, player_id: str) -> dict[str, Any]:
        q = self.api("player_queues/get_active_queue", {"player_id": player_id})
        return q if isinstance(q, dict) else {}

    def queue_items(self, queue_id: str, limit: int = 10) -> list[dict[str, Any]]:
        items = self.api("player_queues/items",
                         {"queue_id": queue_id, "limit": limit})
        return items if isinstance(items, list) else []

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        res = self.api("music/search", {"search_query": query,
                                        "media_types": ["track"],
                                        "limit": limit})
        if isinstance(res, dict):
            return res.get("tracks") or []
        return []


# ------------------------------------------------------------------ 最近播放追踪


class TrackHistory:
    """观测 now_playing 变化，维护最近播放列表（含跳过粗判）。

    MA 没有 recently_played 接口，只能在 gateway 侧自建。
    skip 粗判：上一曲播放 <30s 且未完成 → weak_negative（文档 §16）。
    第一阶段**只记录**，不做策略调整。
    """

    def __init__(self, path: pathlib.Path | None = None, keep: int = 20,
                 event_store=None) -> None:
        self._path = path or HISTORY_PATH
        self._keep = keep
        self._lock = threading.Lock()
        self._tracks: list[dict[str, Any]] = []
        self._last_uri = ""
        self._last_started = 0.0
        self._event_store = event_store      # IMP-09: PlaybackEventStore
        self._current_instance_id = ""
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("op") == "track":
                        self._tracks.append(rec)
            del self._tracks[:-self._keep]
            if self._tracks:
                self._last_uri = self._tracks[-1].get("uri") or ""
        except Exception:
            pass

    def observe(self, uri: str, title: str, artist: str,
                state: str, elapsed: float, duration: float) -> None:
        """每次构建 Context 时调用；uri 变化时记一条。"""
        if not uri or state not in ("playing", "paused"):
            return
        with self._lock:
            if uri == self._last_uri:
                return
            now = time.time()
            # 结算上一曲的隐式信号（只记录）
            if self._tracks and self._last_started:
                prev = self._tracks[-1]
                played = max(0.0, now - self._last_started)
                prev_dur = float(prev.get("duration") or 0)
                if prev_dur > 0:
                    if played >= prev_dur - 5:
                        prev["implicit"] = "complete"
                    elif played < 30:
                        prev["implicit"] = "skip_fast"
                    else:
                        prev["implicit"] = "skip_partial"
                    prev["played_sec"] = round(played, 1)
                    self._write({"op": "implicit", **{k: prev[k] for k in (
                        "uri", "implicit", "played_sec") if k in prev}})
            rec = {"op": "track", "t": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "uri": uri, "title": title, "artist": artist,
                   "duration": duration}
            self._tracks.append(rec)
            # IMP-09：结算旧实例 + 开新实例（playback_events 表）
            if self._event_store:
                if self._current_instance_id:
                    played = (time.time() - self._last_started) if self._last_started else 0
                    reason = "natural_end" if played >= duration * 0.9 else "user_skip"
                    self._event_store.end_instance(self._current_instance_id, reason,
                                                   played, 0)
                self._current_instance_id = self._event_store.start_instance(
                    uri, title, artist)
            del self._tracks[:-self._keep]
            self._last_uri, self._last_started = uri, now
            self._write(rec)

    def _write(self, rec: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self._tracks[-n:]][::-1]


# ------------------------------------------------------------------ Context 构建


# MA 对缺少 ID3 标签的本地文件会填这些占位艺术家名。
# ⚠️ 2026-09-13 实测：本地库大量文件标签不全，now_playing.artist 直接变成 "[unknown]"，
#    喂给 LLM 只会污染判断，这里统一清空（宁可空，也别给假信息）。
_ARTIST_PLACEHOLDERS = {"[unknown]", "unknown", "未知", "未知艺术家", "various artists"}


def _track_brief(item: dict[str, Any]) -> dict[str, Any]:
    media = item.get("media_item") or item
    names = [(a.get("name") or "").strip() for a in (media.get("artists") or [])]
    names = [n for n in names if n and n.lower() not in _ARTIST_PLACEHOLDERS]
    return {
        "title": media.get("name") or item.get("name") or "",
        "artist": " / ".join(names),
        "uri": media.get("uri") or item.get("uri") or "",
        "duration": media.get("duration") or item.get("duration") or 0,
    }


def build_context(ma: MAClient, history: TrackHistory,
                  session: dict[str, Any], max_tracks: int = 20) -> dict[str, Any]:
    """组装喂给 Planner 的 Music Context（文档 §11 的字段结构）。"""
    player = ma.pick_player()
    pid = player.get("player_id") or ""
    q = ma.queue_state(pid) if pid else {}

    cur = q.get("current_item") or {}
    now_item = _track_brief(cur.get("media_item") or cur) if cur else {}
    state = q.get("state") or "idle"
    elapsed = float(q.get("elapsed_time") or 0)

    if now_item.get("uri"):
        history.observe(now_item["uri"], now_item.get("title", ""),
                        now_item.get("artist", ""), state, elapsed,
                        float(now_item.get("duration") or 0))

    # 后续队列摘要（当前 index 之后的曲目）
    upcoming: list[dict[str, str]] = []
    qid = q.get("queue_id") or pid
    if qid:
        idx = int(q.get("current_index") or 0)
        for it in ma.queue_items(qid, limit=max_tracks + idx + 1):
            si = int(it.get("sort_index") or 0)
            if si > idx:
                b = _track_brief(it)
                upcoming.append({"title": b["title"], "artist": b["artist"]})
            if len(upcoming) >= 5:
                break

    return {
        "player": player.get("name") or "",
        "player_id": pid,
        "state": state,
        "volume": player.get("volume_level"),
        "elapsed_sec": round(elapsed, 1),
        "now_playing": now_item,
        "queue_summary": {
            "items_total": q.get("items") or 0,
            "current_index": q.get("current_index") or 0,
            "upcoming": upcoming,
        },
        "recent_tracks": [
            {"title": t.get("title"), "artist": t.get("artist"),
             "uri": t.get("uri"), "implicit": t.get("implicit", "")}
            for t in history.recent(6)
        ],
        "session": {
            "session_id": session.get("session_id"),
            "scene": session.get("scene"),
            "goal": session.get("goal"),
            "planned_duration_min": session.get("planned_duration_min"),
            "constraints": session.get("constraints"),
        },
        "conversation": (session.get("conversation") or [])[-10:],
    }
