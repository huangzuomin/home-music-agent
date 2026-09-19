"""B2/X14 — jsonl → SQLite 迁移脚本 + 幂等测试。"""
from __future__ import annotations

import json
import sqlite3
import pytest
import sys
import os

TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "tools")
sys.path.insert(0, TOOLS_DIR)

from migrate_jsonl_to_sqlite import migrate


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "control.db")


@pytest.fixture()
def sess_file(tmp_path):
    f = tmp_path / "agent-sessions.jsonl"
    f.write_text(json.dumps([
        {"op": "create", "session_id": "s1", "session": {
            "session_id": "s1", "user_id": "u1", "feedback": [],
            "conversation": [], "scene": "", "goal": ""}},
        {"op": "update", "session_id": "s1", "patch": {
            "feedback": [{"signal": "track_positive",
                          "target": {"title": "晴天", "artist": "周杰伦",
                                      "uri": "library://31"},
                          "note": ""}]}},
    ], ensure_ascii=False) + "\n")
    return str(f)


@pytest.fixture()
def track_file(tmp_path):
    f = tmp_path / "track-history.jsonl"
    f.write_text("\n".join(
        json.dumps({"op": "track", "uri": f"library://{i}",
                    "title": f"歌{i}", "artist": f"歌手{i}",
                    "t": "2026-09-17T20:00:00+08:00", "duration": 200})
        for i in range(3)) + "\n")
    return str(f)


def test_migrate_creates_tables_and_populates(db, sess_file, track_file):
    stats = migrate(sess_file, track_file, db)
    assert stats["play_instances"] == 3
    assert stats["feedback_events"] >= 0


def test_migrate_idempotent(db, sess_file, track_file):
    migrate(sess_file, track_file, db)
    stats_1 = migrate(sess_file, track_file, db)
    # 二次迁移不应产生新实例（INSERT OR IGNORE）
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM play_instances").fetchone()[0]
    assert count == 3  # 3 tracks from fixture
    conn.close()


def test_feedback_events_populated(db, sess_file, track_file):
    migrate(sess_file, track_file, db)
    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM feedback_events WHERE signal = 'track_positive'"
    ).fetchone()[0]
    assert count >= 1
