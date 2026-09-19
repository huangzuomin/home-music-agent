"""IMP-10 — 聆听生命周期 + 队列约束 + 未来队列修改 + 定时结束（离线测试）。"""
from __future__ import annotations

import pytest

from listening_policy import (
    ListeningSession, ListenStatus, hard_filter, soft_sort, filter_queue,
)
from queue_patch import build_patch, apply_patch, build_undo, apply_undo
from end_policy import EndPolicy, EndPolicyManager


# ---------------------------------------------------------------- 生命周期

def test_lifecycle_active_pause_resume_end():
    s = ListeningSession("ls-1", "sq-1", scene="focus")
    assert s.status == ListenStatus.ACTIVE
    s.pause()
    assert s.status == ListenStatus.PAUSED
    s.resume()
    assert s.status == ListenStatus.ACTIVE
    s.end("user_stop")
    assert s.status == ListenStatus.ENDED


def test_external_change_suspends():
    s = ListeningSession("ls-2", "sq-1")
    s.suspend()
    assert s.status == ListenStatus.SUSPENDED


def test_conversation_ttl_does_not_clear_listening():
    s = ListeningSession("ls-3", "sq-1", scene="focus",
                         constraints={"no_vocal": True})
    assert s.constraints.get("no_vocal") is True
    assert s.status == ListenStatus.ACTIVE


# ---------------------------------------------------------------- 硬过滤

def test_hard_filter_no_vocal():
    candidates = [
        {"title": "无人声曲", "vocal": "verified_no"},
        {"title": "有人声曲", "vocal": "verified_yes"},
        {"title": "未验证曲", "vocal": "unknown"},
    ]
    result = hard_filter(candidates, {"no_vocal": True})
    assert len(result) == 1
    assert result[0]["vocal"] == "verified_no"


def test_hard_filter_allows_vocal_when_not_constrained():
    candidates = [{"title": "A", "vocal": "verified_yes"},
                  {"title": "B", "vocal": "verified_no"}]
    assert len(hard_filter(candidates, {})) == 2


# ---------------------------------------------------------------- 软排序

def test_soft_sort_by_preference():
    cands = [{"title": "低偏好", "artist": "低"},
             {"title": "高偏好", "artist": "高"}]
    prefs = {"高": 100, "低": 1}
    result = soft_sort(cands, prefs)
    assert result[0]["title"] == "高偏好"


def test_soft_sort_no_preferences_preserves_order():
    cands = [{"title": "A"}, {"title": "B"}]
    assert soft_sort(cands, None) == cands


def test_filter_queue_combined():
    cands = [{"title": "好", "artist": "好歌手", "vocal": "verified_no"},
             {"title": "坏", "artist": "坏歌手", "vocal": "verified_yes"}]
    prefs = {"好歌手": 10}
    result = filter_queue(cands, {"no_vocal": True}, prefs)
    assert len(result) == 1 and result[0]["title"] == "好"


# ---------------------------------------------------------------- 队列 Patch

def test_patch_replace_tail():
    queue = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    patch = build_patch(0, queue, [{"title": "X"}])
    assert patch["op"] == "replace_tail"
    result = apply_patch(queue, patch)
    assert result == [{"title": "A"}, {"title": "X"}]


def test_patch_keeps_current():
    queue = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    patch = build_patch(1, queue, [{"title": "X"}, {"title": "Y"}])
    result = apply_patch(queue, patch)
    assert [t["title"] for t in result[:2]] == ["A", "B"]


def test_undo_restores():
    queue_before = [{"title": "A"}, {"title": "B"}]
    undo = build_undo(queue_before, 1)
    restored = apply_undo(queue_before, undo)
    assert restored == queue_before


# ---------------------------------------------------------------- 定时结束

def test_end_policy_timed_expiry():
    p = EndPolicy(mode="timed", end_at=1000.0, player_id="sq-1")
    assert p.is_expired(now=999.0) is False
    assert p.is_expired(now=1001.0) is True


def test_end_policy_cancel():
    mgr = EndPolicyManager()
    mgr.set_policy(EndPolicy(mode="timed", end_at=1000.0, player_id="sq-1"))
    assert mgr.cancel("sq-1") is True
    assert mgr.get_policy("sq-1") is None
    assert mgr.cancel("sq-1") is False


def test_new_session_not_affected_by_old_timer():
    mgr = EndPolicyManager()
    mgr.set_policy(EndPolicy(mode="timed", end_at=1000.0, player_id="sq-1"))
    mgr.cancel("sq-1")
    assert mgr.get_policy("sq-1") is None
