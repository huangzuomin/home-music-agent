#!/usr/bin/env python3
"""B2：jsonl → SQLite 一次性迁移脚本（X14 前半）。

从 data/sessions/agent-sessions.jsonl + track-history.jsonl 读取历史数据，
写入 playback_events + feedback_events + play_instances SQLite 表。
重复执行不产生额外写入（幂等）。旧 jsonl 只读保留。
"""
from __future__ import annotations

import json
import sqlite3
import os
import sys
from pathlib import Path


def migrate(sessions_jsonl: str, track_jsonl: str, control_db: str) -> dict:
    stats = {"play_instances": 0, "feedback_events": 0, "skipped": 0}
    conn = sqlite3.connect(control_db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
CREATE TABLE IF NOT EXISTS play_instances (
  instance_id TEXT PRIMARY KEY, uri TEXT, title TEXT, artist TEXT,
  started_at TEXT, ended_at TEXT, end_reason TEXT,
  effective_sec REAL DEFAULT 0, paused_sec REAL DEFAULT 0,
  device_id TEXT, session_id TEXT
);
CREATE TABLE IF NOT EXISTS feedback_events (
  event_id TEXT PRIMARY KEY, device_id TEXT, session_id TEXT,
  signal TEXT, target_json TEXT, note TEXT,
  t_epoch REAL, scope TEXT DEFAULT 'public',
  source TEXT DEFAULT 'migration', legacy_uncertain INTEGER DEFAULT 0
);
""")
    conn.commit()
    # 建表（play_instances / feedback_events 可能尚不存在）
    conn.executescript("""
CREATE TABLE IF NOT EXISTS play_instances (
  instance_id TEXT PRIMARY KEY, uri TEXT, title TEXT, artist TEXT,
  started_at TEXT, ended_at TEXT, end_reason TEXT,
  effective_sec REAL DEFAULT 0, paused_sec REAL DEFAULT 0,
  device_id TEXT, session_id TEXT
);
CREATE TABLE IF NOT EXISTS feedback_events (
  event_id TEXT PRIMARY KEY, device_id TEXT, session_id TEXT,
  signal TEXT, target_json TEXT, note TEXT,
  t_epoch REAL, scope TEXT DEFAULT 'public',
  source TEXT DEFAULT 'migration', legacy_uncertain INTEGER DEFAULT 0
);
""")
    conn.commit()

    # ---- track-history → play_instances ----
    if Path(track_jsonl).exists():
        instances = []
        for line in open(track_jsonl, encoding="utf-8"):
            rec = json.loads(line.strip())
            if rec.get("op") != "track":
                continue
            instances.append(rec)
        for i, rec in enumerate(instances):
            iid = f"migrated-{i:04d}"
            conn.execute(
                "INSERT OR IGNORE INTO play_instances"
                " (instance_id, uri, title, artist, started_at, device_id, session_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (iid, rec.get("uri") or "", rec.get("title") or "",
                 rec.get("artist") or "", rec.get("t") or "",
                 "legacy-migration", ""))
            stats["play_instances"] += 1

    # ---- agent-sessions → feedback_events ----
    sess_file = Path(sessions_jsonl)
    if sess_file.exists():
        seen_event = set()
        seen_identity = set()
        for line in open(sess_file, encoding="utf-8"):
            rec = json.loads(line.strip())
            fbs = []
            if rec.get("op") == "create":
                fbs = (rec.get("session") or {}).get("feedback") or []
            elif rec.get("op") == "update":
                fbs = (rec.get("patch") or {}).get("feedback") or []
            for fb in fbs:
                import hashlib
                identity = json.dumps(
                    {"signal": fb.get("signal"),
                     "target": fb.get("target"),
                     "note": fb.get("note")},
                    ensure_ascii=False, sort_keys=True)
                fp = hashlib.sha256(identity.encode()).hexdigest()[:16]
                if fp in seen_identity:
                    stats["skipped"] += 1
                    continue
                seen_identity.add(fp)
                eid = "mig-" + fp
                if eid in seen_event:
                    continue
                seen_event.add(eid)
                conn.execute(
                    "INSERT OR IGNORE INTO feedback_events"
                    " (event_id, device_id, session_id, signal, target_json,"
                    "  note, t_epoch, scope, source, legacy_uncertain)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (eid, "legacy-migration", rec.get("session_id") or "",
                     fb.get("signal") or "",
                     json.dumps(fb.get("target") or {}, ensure_ascii=False),
                     fb.get("note") or "", 0.0, "public", "legacy", 1))
                stats["feedback_events"] += 1
        conn.commit()

    conn.close()
    return stats


if __name__ == "__main__":
    sessions_jsonl = sys.argv[1] if len(sys.argv) > 1 else "agent-sessions.jsonl"
    track_jsonl = sys.argv[2] if len(sys.argv) > 2 else "track-history.jsonl"
    control_db = sys.argv[3] if len(sys.argv) > 3 else "control.db"
    stats = migrate(sessions_jsonl, track_jsonl, control_db)
    print(f"migrated: {stats}")
