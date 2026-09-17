"""tools.plan 的契约样例（当前行为，全部应通过）。

这些测试锁定「意图 → 执行计划」的翻译契约；IMP-02 若调整
music_fetch 的 play 语义，应在本文件同步更新并注明任务号。
"""
from __future__ import annotations

import pytest

import tools as tools_mod


def test_music_play_plan_maps_to_ha_script():
    plan = tools_mod.plan("music_play", {"query": "周杰伦"})
    assert plan["kind"] == "script"
    assert plan["script"] == "music_play_query"
    assert plan["variables"]["query"] == "周杰伦"


def test_music_play_requires_query():
    with pytest.raises(tools_mod.ToolError):
        tools_mod.plan("music_play", {"query": ""})


def test_music_transport_invalid_action_rejected():
    with pytest.raises(tools_mod.ToolError):
        tools_mod.plan("music_transport", {"action": "eject"})


def test_music_transport_volume_set_requires_level():
    with pytest.raises(tools_mod.ToolError):
        tools_mod.plan("music_transport", {"action": "volume_set"})


def test_music_transport_next_maps_to_music_next():
    plan = tools_mod.plan("music_transport", {"action": "next"})
    assert plan["script"] == "music_next"


def test_music_fetch_no_autoplay_and_honest_say():
    """IMP-02 契约：补库请求不再携带自动播放；播报如实说明只入库。"""
    plan = tools_mod.plan("music_fetch", {"query": "陈奕迅 十年"})
    assert plan["script"] == "music_backfill"
    assert plan["variables"].get("play") in (False, None), (
        "自动播放已阻断（G01/T03）：music_fetch 不得请求播放")
    assert "自动播放" in plan["say"] or "不会自动" in plan["say"]
