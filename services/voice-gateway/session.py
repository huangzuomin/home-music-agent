#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 升级（v0.3）— Music Session 存储。

设计（需求文档 §12）：
    每次听音乐不是一堆无关命令，而是一段 Session。Session 承载场景目标、
    约束、反馈与最近对话，是 Agent 指代解析（"这个/刚才/还是/再"）的根基。

实现取舍（Sprint 1 拍板）：
    * 进程内字典 + jsonl 追加持久化，**不引入 Redis**。单用户单进程足够。
    * jsonl 只用于重启后回放恢复与事后分析，不做随机读取。
    * Session 空闲超过 ``idle_ttl_sec`` 视为结束：下次发言自动开新 Session。

会话 id 约定：
    * 客户端可传 session_id（如 "living-room-current"）；
    * 传 "auto" 或不传时，按 user_id 取「当前活跃 Session」，没有则新建。
"""
from __future__ import annotations

import json
import pathlib
import threading
import time
import uuid
from typing import Any

STORE_PATH = pathlib.Path("/app/data/sessions/agent-sessions.jsonl")


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


class SessionStore:
    """线程安全的 Session 仓库。"""

    def __init__(self, path: pathlib.Path | None = None,
                 idle_ttl_sec: float = 1800.0,
                 max_turns: int = 10) -> None:
        self._path = path or STORE_PATH
        self._idle_ttl = idle_ttl_sec
        self._max_turns = max_turns
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        # user_id -> 当前活跃 session_id
        self._active: dict[str, str] = {}
        self._replay()

    # ---------------------------------------------------------- 持久化

    def _append(self, rec: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": _iso(_now()), **rec}
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 持久化失败不阻塞主链路

    def _replay(self) -> None:
        """启动时回放 jsonl，恢复未过期的 Session。"""
        if not self._path.exists():
            return
        now = _now()
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    op, sid = rec.get("op"), rec.get("session_id")
                    if not sid:
                        continue
                    if op == "create":
                        self._sessions[sid] = rec.get("session") or {}
                        uid = self._sessions[sid].get("user_id")
                        if uid:
                            self._active[uid] = sid
                    elif op == "update" and sid in self._sessions:
                        self._sessions[sid].update(rec.get("patch") or {})
        except Exception:
            return
        # 清掉已过期会话，避免回放无限增长
        expired = [sid for sid, s in self._sessions.items()
                   if now - float(s.get("last_active", 0)) > self._idle_ttl]
        for sid in expired:
            self._sessions.pop(sid, None)
        for uid, sid in list(self._active.items()):
            if sid not in self._sessions:
                self._active.pop(uid, None)

    # ---------------------------------------------------------- 会话操作

    def _new_session(self, user_id: str) -> dict[str, Any]:
        now = _now()
        sid = "%s-%s" % (time.strftime("%Y%m%d-%H%M", time.localtime(now)),
                         uuid.uuid4().hex[:6])
        sess: dict[str, Any] = {
            "session_id": sid,
            "user_id": user_id,
            "scene": "",
            "goal": "",
            "planned_duration_min": 0,
            "constraints": {},
            "feedback": [],
            "conversation": [],
            "created_at": now,
            "last_active": now,
        }
        self._sessions[sid] = sess
        self._active[user_id] = sid
        self._append({"op": "create", "session_id": sid, "session": sess})
        return sess

    def get(self, session_id: str, user_id: str = "default") -> tuple[dict[str, Any], bool]:
        """取会话。返回 (session, is_new)。

        session_id 为 "auto"/"" 时：取该用户当前活跃会话；过期或不存在则新建。
        """
        with self._lock:
            now = _now()
            if session_id and session_id != "auto":
                sess = self._sessions.get(session_id)
                if sess is None:
                    sess = self._new_named(session_id, user_id)
                    return sess, True
                if now - float(sess.get("last_active", 0)) > self._idle_ttl:
                    # 指定 id 但已过期 → 复用 id 重开一段（保留 id 便于客户端固定）
                    sess = self._new_named(session_id, user_id)
                    return sess, True
                return sess, False

            sid = self._active.get(user_id)
            sess = self._sessions.get(sid) if sid else None
            if sess is None or now - float(sess.get("last_active", 0)) > self._idle_ttl:
                return self._new_session(user_id), True
            return sess, False

    def _new_named(self, session_id: str, user_id: str) -> dict[str, Any]:
        """创建**以调用方 id 命名**的会话。

        ⚠️ IMP-02 修复（T21）：此前先建自动 id 会话（create 记录用自动 id）
        再改名（update 记录用命名 id）——_replay() 回放时 update 因
        ``sid not in self._sessions`` 被跳过，命名会话重启后上下文全部丢失。
        现在 create 记录直接使用命名 id。
        """
        now = _now()
        sess: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "scene": "",
            "goal": "",
            "planned_duration_min": 0,
            "constraints": {},
            "feedback": [],
            "conversation": [],
            "created_at": now,
            "last_active": now,
        }
        self._sessions[session_id] = sess
        self._active[user_id] = session_id
        self._append({"op": "create", "session_id": session_id, "session": sess})
        return sess

    def touch(self, sess: dict[str, Any]) -> None:
        with self._lock:
            sess["last_active"] = _now()

    def append_turn(self, sess: dict[str, Any], user_text: str,
                    intent: str, reply: str) -> None:
        """记录一轮对话（截断到 max_turns）。"""
        with self._lock:
            conv = sess.setdefault("conversation", [])
            conv.append({
                "t": _iso(_now()),
                "user": (user_text or "")[:200],
                "intent": intent,
                "reply": (reply or "")[:200],
            })
            del conv[:-self._max_turns]
            sess["last_active"] = _now()
            self._append({"op": "update", "session_id": sess["session_id"],
                          "patch": {"conversation": conv,
                                    "last_active": sess["last_active"]}})

    def update_scene(self, sess: dict[str, Any], scene: str = "",
                     goal: str = "", duration_min: int = 0,
                     constraints: dict[str, Any] | None = None) -> None:
        """Planner 推断出场景/目标/约束后回写 Session。"""
        with self._lock:
            if scene:
                sess["scene"] = scene
            if goal:
                sess["goal"] = goal
            if duration_min:
                sess["planned_duration_min"] = duration_min
            if constraints:
                merged = dict(sess.get("constraints") or {})
                merged.update(constraints)
                sess["constraints"] = merged
            sess["last_active"] = _now()
            self._append({"op": "update", "session_id": sess["session_id"],
                          "patch": {"scene": sess.get("scene"),
                                    "goal": sess.get("goal"),
                                    "planned_duration_min":
                                        sess.get("planned_duration_min"),
                                    "constraints": sess.get("constraints")}})

    def add_feedback(self, sess: dict[str, Any], signal: str,
                     target: dict[str, Any], note: str = "") -> None:
        with self._lock:
            fb = sess.setdefault("feedback", [])
            # IMP-02（T12）：每条反馈带全局唯一 event_id；读取侧（taste）按
            # event_id 去重，消除累计快照 patch 的重复计分。
            fb.append({"t": _iso(_now()), "event_id": uuid.uuid4().hex,
                       "signal": signal,
                       "target": target, "note": (note or "")[:200]})
            del fb[:-50]  # 单 session 反馈上限，防爆
            sess["last_active"] = _now()
            self._append({"op": "update", "session_id": sess["session_id"],
                          "patch": {"feedback": fb}})

    # ---------------------------------------------------------- 调试

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"active": dict(self._active),
                    "sessions": {sid: {k: v for k, v in s.items()}
                                 for sid, s in self._sessions.items()}}
