"""T15 / T17 —— 闭麦语义与静音恢复（当前为缺陷，xfail 复现）。

对应 review：G05；修复归属 IMP-02（语义拆出）与 IMP-03/04（输入端控制）。
"""
from __future__ import annotations

import pytest

import app as vg_app


def test_t15_mic_mute_is_not_music_mute():
    """说「闭麦」是**输入设备**控制，与音乐静音是两件事。

    IMP-02 契约：网关显式返回 mic_control_unsupported（MIC_CONTROL_UNSUPPORTED），
    并提示使用设备关闭键；不执行任何音乐动作。
    """
    r = vg_app.parse_volume_command("闭麦")
    assert r["intent"] == "mic_control_unsupported", r
    assert r["script"] is None, "闭麦不得执行任何音乐动作"
    assert "设备" in r["say"], "应提示使用设备关闭键"


def test_t15_unmute_mic_also_unsupported():
    r = vg_app.parse_volume_command("取消闭麦")
    assert r["intent"] == "mic_control_unsupported", r
    assert r["script"] is None


def test_t15_music_mute_still_works():
    """音乐静音本身仍然可用（音量 0），但语义标记为 music_mute。"""
    r = vg_app.parse_volume_command("静音")
    assert r["intent"] == "music_mute"
    assert r["script"] == "music_volume_set"
    assert r["variables"]["level"] == 0


def test_t17_unmute_intent_has_no_fixed_level():
    """IMP-02 契约：取消静音的 level 由执行段按「静音前音量」解析，
    计划本身不再固定 40。"""
    r = vg_app.parse_volume_command("取消静音")
    assert r["intent"] == "music_unmute"
    assert r["script"] == "music_volume_set"
    assert "level" not in r["variables"], "固定 level 会冒充恢复，已废除"


def test_t17_resolve_unmute_level_known_restore():
    assert vg_app.resolve_unmute_level(88.0) == (88, True)
    assert vg_app.resolve_unmute_level(30.0) == (30, True)


def test_t17_resolve_unmute_level_unknown_falls_to_default():
    level, known = vg_app.resolve_unmute_level(None)
    assert (level, known) == (vg_app.UNMUTE_LEVEL, False)
    level, known = vg_app.resolve_unmute_level(0.0)   # 0 = 静音中，不算已知值
    assert (level, known) == (vg_app.UNMUTE_LEVEL, False)
