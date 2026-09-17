"""IMP-09 — 播放实例与反馈事件存储（离线测试）。"""
from __future__ import annotations

import pytest

from playback_events import PlaybackEventStore, END_REASONS
from feedback_store import FeedbackStore


@pytest.fixture()
def pe(tmp_path):
    return PlaybackEventStore(str(tmp_path / "pe.db"))


@pytest.fixture()
def fb(tmp_path):
    return FeedbackStore(str(tmp_path / "fb.db"))


# ---------------------------------------------------------------- 播放实例

def test_instance_lifecycle(pe):
    iid = pe.start_instance("library://a", "A", "歌手A", "dev-1", "s-1")
    pe.end_instance(iid, "natural_end", effective_sec=200, paused_sec=30)
    inst = pe.get_instance(iid)
    assert inst["end_reason"] == "natural_end"
    assert inst["effective_sec"] == 200
    assert inst["paused_sec"] == 30


def test_same_uri_different_instances(pe):
    i1 = pe.start_instance("library://a", "A", "歌手A")
    i2 = pe.start_instance("library://a", "A", "歌手A")
    assert i1 != i2, "同一 URI 的两次播放应为不同实例"


def test_end_reasons_complete():
    from playback_events import END_REASONS
    assert END_REASONS == frozenset({
        "natural_end", "user_skip", "user_stop", "scene_change",
        "playback_error", "external_change", "unknown"})


def test_recent_returns_newest_first(pe):
    for i in range(5):
        pe.start_instance(f"library://{i}", f"歌{i}", f"歌手{i}")
    recent = pe.recent(3)
    assert len(recent) == 3
    assert recent[0]["uri"] >= recent[1]["uri"]


# ---------------------------------------------------------------- 反馈事件

def test_feedback_idempotent(fb):
    e1 = fb.record("dev-1", "s-1", "track_positive",
                   target={"title": "晴天"}, event_id="evt-1")
    e2 = fb.record("dev-1", "s-1", "track_positive",
                   target={"title": "晴天"}, event_id="evt-1")
    assert e1 == e2, "相同 event_id 应幂等"
    assert len(fb.by_session("s-1")) == 1


def test_feedback_revoke(fb):
    fb.record("dev-1", "s-1", "track_positive",
              target={"title": "晴天"}, event_id="evt-r1")
    assert fb.revoke("evt-r1", "dev-1") is True
    assert len(fb.by_session("s-1")) == 0


def test_feedback_scope_isolation(fb):
    fb.record("dev-1", "s-1", "track_positive",
              target={"title": "A"}, event_id="e1", scope="private")
    assert len(fb.by_session("s-1")) == 1
