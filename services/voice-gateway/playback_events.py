#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-09 — 播放实例与事件存储（计划 §4.6 / D→M2）。

核心语义（区别于旧 jsonl 快照）：
    * Play Instance = 同一 URI 在不同时间的一次播放（每次新 UUID）
    * Playback Event = 一个带原因和时间的不可变事实
    * 暂停不计入有效播放时长
    * 结束原因区分 natural_end / user_skip / user_stop / scene_change /
      playback_error / external_change / unknown
    * 外部换歌无法归因 → unknown，不自动当成负反馈
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any


class PlaybackEventStore:
    """播放实例与事件存储（SQLite）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local = threading.local()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self._conn()
        conn.executescript("""
CREATE TABLE IF NOT EXISTS play_instances (
  instance_id  TEXT PRIMARY KEY,
  uri          TEXT NOT NULL,
  title        TEXT,
  artist       TEXT,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  end_reason   TEXT,                       -- natural_end/user_skip/user_stop/...
  effective_sec REAL DEFAULT 0,           -- 不含暂停
  paused_sec   REAL DEFAULT 0,
  device_id    TEXT,
  session_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pi_uri ON play_instances (uri);
CREATE INDEX IF NOT EXISTS idx_pi_session ON play_instances (session_id);

CREATE TABLE IF NOT EXISTS playback_events (
  event_id    TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  event_type  TEXT NOT NULL,              -- play/pause/resume/skip/end
  t_epoch     REAL NOT NULL,
  detail_json TEXT
);
""")
        conn.commit()

    # ------------------------------------------------------------ 实例

    def start_instance(self, uri: str, title: str, artist: str,
                       device_id: str = "", session_id: str = "") -> str:
        iid = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        self._conn().execute(
            "INSERT INTO play_instances (instance_id, uri, title, artist,"
            " started_at, device_id, session_id) VALUES (?,?,?,?,?,?,?)",
            (iid, uri, title, artist, now, device_id, session_id))
        self._conn().commit()
        return iid

    def end_instance(self, instance_id: str, end_reason: str,
                     effective_sec: float, paused_sec: float = 0.0) -> None:
        self._conn().execute(
            "UPDATE play_instances SET ended_at = ?, end_reason = ?,"
            " effective_sec = ?, paused_sec = ? WHERE instance_id = ?",
            (datetime.now(timezone.utc).isoformat(), end_reason,
             effective_sec, paused_sec, instance_id))
        self._conn().commit()

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM play_instances WHERE instance_id = ?",
            (instance_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM play_instances ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 事件
    def record_event(self, instance_id: str, event_type: str,
                     detail: dict[str, Any] | None = None) -> str:
        eid = uuid.uuid4().hex
        now = datetime.now(timezone.utc).timestamp()
        self._conn().execute(
            "INSERT INTO playback_events (event_id, instance_id, event_type,"
            " t_epoch, detail_json) VALUES (?,?,?,?,?,?)",
            (eid, instance_id, event_type, now,
             json.dumps(detail or {}, ensure_ascii=False)))
        self._conn().commit()
        return eid


END_REASONS = frozenset({
    "natural_end", "user_skip", "user_stop", "scene_change",
    "playback_error", "external_change", "unknown",
})
