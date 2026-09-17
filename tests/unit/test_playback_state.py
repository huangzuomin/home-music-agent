"""IMP-03b — PlaybackState：快照字段、freshness、离线如实呈现。"""
from __future__ import annotations

import pytest

from playback_state import build_snapshot


class StubReader:
    """MAReader 形状的离线桩（实测响应形状）。"""

    def __init__(self, player: dict | None, queue: dict | None) -> None:
        self._player = player
        self._queue = queue or {}

    def get_player(self, player_id: str):
        return self._player

    def get_active_queue(self, player_id: str):
        return self._queue

    def queue_items(self, queue_id: str, limit: int = 10):
        return (self._queue.get("items_list") or [])[:limit]


PLAYING = {
    "player_id": "sq-1", "name": "Squeezebox Touch", "available": True,
    "state": "playing", "volume_level": 82.0, "type": "squeezebox",
}
QUEUE_PLAYING = {
    "queue_id": "q-sq-1", "state": "playing", "elapsed_time": 42.0,
    "current_index": 0, "items": 5,
    "current_item": {"uri": "library://track/31",
                     "media_item": {"name": "晴天", "duration": 269.3,
                                    "artists": [{"name": "周杰伦"}]}},
    "next_item": {"uri": "library://track/41", "name": "十年"},
}
OFFLINE = {"player_id": "sq-1", "name": "Squeezebox Touch",
           "available": False, "state": "unavailable"}


def test_snapshot_fields_and_freshness(tmp_path):
    reader = StubReader(PLAYING, QUEUE_PLAYING)
    snap = build_snapshot(reader, "sq-1")
    assert snap.fresh is True and not snap.is_stale()
    assert snap.player_id == "sq-1" and snap.player_name == "Squeezebox Touch"
    assert snap.playback_state == "playing"
    assert snap.volume_level == 82.0
    assert snap.current_item["title"] == "晴天"
    assert snap.current_item["artist"] == "周杰伦"
    assert snap.queue_id == "q-sq-1"
    assert snap.queue_length == 5
    assert snap.next_item["title"] == "十年"
    assert snap.is_stale(now=snap.state_updated_at + 6) is True


def test_snapshot_player_offline_is_honest():
    reader = StubReader(OFFLINE, {"queue_id": "q", "state": "idle"})
    snap = build_snapshot(reader, "sq-1")
    assert snap.available is False
    assert snap.player_id == "sq-1"        # 仍返回绑定目标，不换台
    assert snap.playback_state in ("unavailable", "offline", "unknown")


def test_snapshot_player_missing_is_offline():
    reader = StubReader(None, None)
    snap = build_snapshot(reader, "sq-1")
    assert snap.playback_state == "offline"
    assert snap.available is False
