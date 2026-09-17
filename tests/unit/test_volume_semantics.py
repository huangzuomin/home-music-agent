"""T15 / T17 —— 闭麦语义与静音恢复（当前为缺陷，xfail 复现）。

对应 review：G05；修复归属 IMP-02（语义拆出）与 IMP-03/04（输入端控制）。
"""
from __future__ import annotations

import pytest

import app as vg_app


@pytest.mark.xfail(strict=True, reason="G05/T15：闭麦被误映射成音乐静音，修复在 IMP-02")
def test_t15_mic_mute_is_not_music_mute():
    """说「闭麦」是**输入设备**控制，与音乐静音是两件事。

    契约：在设备端闭麦能力落地前，网关应显式返回「不支持」，
    绝不能把闭麦翻译成 music_volume_set(0) 把音乐也停了。
    """
    r = vg_app.parse_volume_command("闭麦")
    assert r is None, (
        "闭麦不应产生音乐音量动作（实际产生了 %r）——输入控制与音乐静音必须分离"
        % r)


@pytest.mark.xfail(strict=True, reason="G05/T17：取消静音固定跳 40，不恢复之前音量，修复在 IMP-02/03")
def test_t17_unmute_restores_previous_volume(monkeypatch):
    """取消静音应恢复**该播放器静音前的音量**（此前是 88），且服从上限；
    不能固定跳到管理员默认值 40。"""
    monkeypatch.setattr(vg_app, "UNMUTE_LEVEL", 40)
    previous_volume = 88
    r = vg_app.parse_volume_command("取消静音")
    assert r["variables"]["level"] == previous_volume, (
        "取消静音应回到静音前的 %s，实际恢复到 %s"
        % (previous_volume, r["variables"]["level"]))
