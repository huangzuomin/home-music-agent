"""IMP-03a — SQLite 存储与迁移：幂等约束、可重复迁移、重启对账。"""
from __future__ import annotations

import pytest

import os
import sys

sys.path.insert(0, "D:/Work/home-music-agent/services/voice-gateway")

from storage import ControlStore


@pytest.fixture()
def store(tmp_path):
    return ControlStore(db_path=tmp_path / "control.db")


def test_migrate_is_repeatable_and_consistent(store):
    applied_first = store.migrate()          # __init__ 已跑过一次，应为空
    tables = {r["name"] for r in store.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"devices", "player_bindings", "commands",
                "conversation_sessions", "listening_sessions",
                "schema_migrations", "feedback_events", "meta"}
    assert required <= tables
    applied_second = store.migrate()
    assert applied_first == [] and applied_second == []
    count = store.connect().execute(
        "SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
    assert count == 1                        # 只有一个迁移版本
    assert required <= {r["name"] for r in store.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_command_idempotency_same_request(store):
    r1 = store.record_command("dev-1", "req-1", "next", {},
                              player_id="sq-1")
    assert r1["created"] is True and not r1["conflict"]

    # 同 ID 同内容 → 返回同一命令，不新建
    r2 = store.record_command("dev-1", "req-1", "next", {}, player_id="sq-1")
    assert r2["created"] is False and not r2["conflict"]
    assert r2["command_id"] == r1["command_id"]
    row = store.get_command("dev-1", "req-1")
    assert row["status"] == "accepted"       # 未重复执行（T11 的库级前提）


def test_x05_same_request_different_args_conflicts(store):
    store.record_command("dev-1", "req-1", "volume_set",
                         {"level": 30}, player_id="sq-1")
    r = store.record_command("dev-1", "req-1", "volume_set",
                             {"level": 60}, player_id="sq-1")
    assert r["created"] is False
    assert r["conflict"] is True, "同 ID 不同内容必须冲突（X05）"


def test_supersede_pending_respects_epoch_and_exclusion(store):
    a = store.record_command("dev-1", "r-a", "play_now", {}, intent_epoch=1)
    b = store.record_command("dev-1", "r-b", "next", {}, intent_epoch=1)
    n = store.supersede_pending(intent_epoch=1, exclude_command_id=None)
    assert n >= 2
    assert store.get_command("dev-1", "r-a")["status"] == "superseded"
    assert store.get_command("dev-1", "r-b")["status"] == "superseded"


def test_stop_barrier_excludes_stop_command(store):
    """STOP 屏障：使旧计划失效，但停止命令本身不被标记 superseded。"""
    store.record_command("dev-1", "r-play", "play_now", {}, intent_epoch=1)
    stop = store.record_command("dev-1", "r-stop", "stop", {}, intent_epoch=2)
    store.supersede_pending(intent_epoch=1, exclude_command_id=None)
    store.supersede_pending(intent_epoch=2,
                            exclude_command_id=stop["command_id"])
    assert store.get_command("dev-1", "r-play")["status"] == "superseded"
    assert store.get_command("dev-1", "r-stop")["status"] == "accepted"


def test_reconcile_inflight_marks_unknown_not_retry(store):
    """X02 前提：重启后 in-flight 命令标 unknown（非幂等动作不盲重试）。"""
    c = store.record_command("dev-1", "r-next", "next", {}, player_id="sq-1")
    store.update_command_status(c["command_id"], "executing")
    ids = store.reconcile_inflight()
    assert c["command_id"] in ids
    assert store.get_command("dev-1", "r-next")["status"] == "unknown"


def test_bindings_roundtrip(store):
    store.set_binding("default", "sq-1", "Squeezebox Touch")
    b = store.get_binding("default")
    assert b == {"player_id": "sq-1", "player_name": "Squeezebox Touch"}
    assert store.get_binding("no-such-scope") is None
