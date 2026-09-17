"""IMP-03c — CommandCoordinator：幂等、冲突、STOP 屏障、来源裁决、对账。"""
from __future__ import annotations

import uuid

import pytest

import os
import sys

sys.path.insert(0, "D:/Work/home-music-agent/services/voice-gateway")

from contracts import CommandRequest
from coordinator import CommandCoordinator
from storage import ControlStore


class RecordingExecutor:
    """确定性执行器：记录调用，可注入失败/延迟。"""

    def __init__(self, fail_actions: set[str] | None = None) -> None:
        self.fail_actions = fail_actions or set()
        self.calls: list[dict] = []

    def execute(self, action: str, args: dict, player_id: str,
                command_id: str) -> dict:
        rec = {"executed": action not in self.fail_actions,
               "action": action, "args": dict(args or {}),
               "player_id": player_id, "command_id": command_id}
        self.calls.append(rec)
        return rec


@pytest.fixture()
def coord(tmp_path):
    store = ControlStore(db_path=tmp_path / "control.db")
    ex = RecordingExecutor()
    c = CommandCoordinator(store, ex,
                           default_player={"player_id": "sq-1",
                                           "player_name": "Squeezebox Touch"})
    return c, store, ex


def _req(request_id: str, action: str = "next", source: str = "voice",
         args: dict | None = None, device_id: str = "dev-1") -> CommandRequest:
    return CommandRequest(device_id=device_id, request_id=request_id,
                          action=action, args=args or {}, source=source)


def test_t11_idempotent_next_retry_executes_once(coord):
    c, store, ex = coord
    r1 = c.submit(_req("req-next-1", "next"))
    r2 = c.submit(_req("req-next-1", "next"))
    assert r1["status"] == "ok" and r2.get("deduplicated") is True
    assert len([x for x in ex.calls if x["action"] == "next"]) == 1, (
        "同一 request_id 重试不得二次执行（T11）")


def test_x01_stop_barrier_invalidates_pending(coord):
    c, store, ex = coord
    r = c.submit(_req("r-play-a", "play_now"), defer=True)
    assert r["status"].startswith("queued")
    stop = c.submit(_req("r-stop", "stop"))
    assert stop["status"] == "ok"
    assert stop["player_confirmed"] is True
    # 未派发的旧计划：DB 标 superseded，且永不再派发
    assert store.get_command("dev-1", "r-play-a")["status"] == "superseded"
    assert ex.calls and ex.calls[-1]["action"] == "stop"
    assert not any(x["action"] == "play_now" for x in ex.calls)


def test_stop_advances_intent_epoch(coord):
    c, store, ex = coord
    before = c.intent_epoch
    c.submit(_req("r-stop", "stop"))
    assert c.intent_epoch == before + 1, "停止推进播放意图代际（ADR-02）"
    assert int(store.get_meta("intent_epoch")) == before + 1


def test_x03_legacy_source_cannot_play(coord):
    c, store, ex = coord
    r = c.submit(_req("r-cb", "play_now", source="backfill_callback"))
    assert r["status"] == "error" and r["error"] == "PERMISSION_DENIED"
    assert store.get_command("dev-1", "r-cb")["status"] == "failed"
    assert not ex.calls, "被拒动作不得触达执行器"


def test_x05_conflict_same_request_different_args(coord):
    c, store, ex = coord
    r1 = c.submit(_req("r-vol", "volume_set", args={"level": 30}))
    r2 = c.submit(_req("r-vol", "volume_set", args={"level": 60}))
    assert r1["status"] == "ok"
    assert r2["status"] == "error" and r2["error"] == "REQUEST_CONFLICT"
    assert len(ex.calls) == 1, "冲突请求不得二次执行"


def test_t10_pause_not_blocked_by_slow_work(coord):
    """暂停通道独立于慢工作：有未派发慢任务时暂停仍立即执行。"""
    c, store, ex = coord
    c.submit(_req("r-slow", "play_now", source="voice"), defer=True)
    r = c.submit(_req("r-pause", "pause"))
    assert r["status"] == "ok"
    assert any(x["action"] == "pause" for x in ex.calls)
    # 未派发的 play_now 与暂停互不影响（暂停不是 superseded）
    assert store.get_command("dev-1", "r-pause")["status"] == "executing"


def test_t22_target_is_bound_player(coord):
    c, store, ex = coord
    c.submit(_req("r-next", "next"))
    assert ex.calls[-1]["player_id"] == "sq-1", "目标是绑定播放器，不擅自换台"
    assert store.get_command("dev-1", "r-next")["status"] == "executing"


def test_t01_superseded_play_result_at_core(coord):
    """核心层：被取代的 play_now 不得进入执行。"""
    c, store, ex = coord
    r = c.submit(_req("r-play-a", "play_now"), defer=True)
    assert r["status"].startswith("queued")
    c.submit(_req("r-play-b", "volume_set", args={"level": 30}))
    c.flush_pending_superseded()
    assert store.get_command("dev-1", "r-play-a")["status"] == "superseded"
    assert not any(x["action"] == "play_now" for x in ex.calls)
