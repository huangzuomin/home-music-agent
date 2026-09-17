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


def test_music_fetch_current_behavior_autoplay_true():
    """当前行为：music_fetch 请求补库时硬编码 play=True。

    ⚠️ 这正是 G01/T01–T03 的触发层根源之一；IMP-02 阻断自动播放时
    应把本用例与 auto_backfill 的完成回调一并收敛。
    """
    plan = tools_mod.plan("music_fetch", {"query": "陈奕迅 十年"})
    assert plan["script"] == "music_backfill"
    assert plan["variables"]["play"] is True
