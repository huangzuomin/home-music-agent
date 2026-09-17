"""parse_rules / parse_volume_command 的契约样例（当前行为，全部应通过）。"""
from __future__ import annotations

import app as vg_app


def test_next_maps_to_music_next():
    r = vg_app.parse_rules("下一首")
    assert r["script"] == "music_next"


def test_stop_maps_to_music_stop():
    r = vg_app.parse_rules("停止播放")
    assert r["script"] == "music_stop"


def test_volume_set_number():
    r = vg_app.parse_rules("音量调到30")
    assert r["intent"] == "volume_set"
    assert r["variables"]["level"] == 30


def test_mute_word_does_not_hit_when_describing_music():
    """「安静音乐」不能被 mute 规则的「静音」子串误命中（实测踩过的坑）。"""
    r = vg_app.parse_rules("来点安静音乐")
    assert r.get("intent") != "volume_set"


def test_unmute_maps_to_configured_level():
    """当前行为：取消静音恢复到固定 UNMUTE_LEVEL（缺陷本身见 T17 的 xfail）。"""
    r = vg_app.parse_volume_command("取消静音")
    assert r["variables"]["level"] == vg_app.UNMUTE_LEVEL
