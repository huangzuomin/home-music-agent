#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-09 — 反馈事件存储（event_id 幂等 + scope 撤销 + 旧数据兼容）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any


class FeedbackStore:
    """显式反馈事件存储（event_id 唯一，支持撤销，scope 隔离）。"""

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
CREATE TABLE IF NOT EXISTS feedback_events (
  event_id    TEXT PRIMARY KEY,
  device_id   TEXT,
  session_id  TEXT,
  signal      TEXT NOT NULL,
  target_json TEXT,
  note        TEXT,
  t_epoch     REAL NOT NULL,
  scope       TEXT NOT NULL DEFAULT 'public',
  source      TEXT NOT NULL DEFAULT 'event',
  legacy_uncertain INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fe_session ON feedback_events (session_id);
CREATE INDEX IF NOT EXISTS idx_fe_scope ON feedback_events (scope);
""")
        conn.commit()

    def record(self, device_id: str, session_id: str, signal: str,
               target: dict | None = None, note: str = "",
               event_id: str | None = None, scope: str = "public") -> str:
        eid = event_id or uuid.uuid4().hex
        self._conn().execute(
            "INSERT OR IGNORE INTO feedback_events"
            " (event_id, device_id, session_id, signal, target_json,"
            "  note, t_epoch, scope, source)"
            " VALUES (?,?,?,?,?,?,?,?, 'event')",
            (eid, device_id, session_id, signal,
             json.dumps(target or {}, ensure_ascii=False),
             note, time.time(), scope))
        self._conn().commit()
        return eid

    def revoke(self, event_id: str, device_id: str = "") -> bool:
        cur = self._conn().execute(
            "DELETE FROM feedback_events WHERE event_id = ?"
            " AND (? = '' OR device_id = ?)",
            (event_id, device_id, device_id))
        self._conn().commit()
        return cur.rowcount > 0

    def by_session(self, session_id: str, limit: int = 50) -> list[dict]:
        rows = self._conn().execute(
            "SELECT event_id, signal, target_json, note, t_epoch"
            " FROM feedback_events WHERE session_id = ?"
            " ORDER BY t_epoch DESC LIMIT ?",
            (session_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["target"] = json.loads(d.pop("target_json") or "{}")
            out.append(d)
        return out
