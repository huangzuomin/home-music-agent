"""T14 —— 暂停时长不计入有效播放（当前为缺陷，xfail 复现）。

对应 review：G04（画像数据采得不全）。
根因（源码级）：context.TrackHistory.observe 用 ``now - last_started``
（挂钟时间）计算 played_sec；「paused」状态只是同 uri 早退，暂停的
整段时间全部被算成播放 → complete/skip_partial 误判 + 画像虚增。
修复归属 IMP-09（播放实例与暂停感知）。
"""
from __future__ import annotations

import pytest

import context as context_mod

from tests.fakes.fake_clock import FakeClock


@pytest.mark.xfail(strict=True, reason="G04/T14：暂停时长被计入有效播放，修复在 IMP-09")
def test_t14_pause_time_not_counted(monkeypatch, tmp_path):
    clock = FakeClock(start=1_000_000.0)
    monkeypatch.setattr(context_mod, "time", clock)

    hist = context_mod.TrackHistory(
        path=tmp_path / "track-history.jsonl", keep=20)

    # A（时长 700s）开始播放
    hist.observe("library://a", "A", "歌手A", "playing", 0, 700)
    clock.advance(120)                                              # 播 120s
    hist.observe("library://a", "A", "歌手A", "paused", 120, 700)    # 暂停
    clock.advance(600)                                              # 暂停 600s
    hist.observe("library://a", "A", "歌手A", "playing", 120, 700)   # 继续
    clock.advance(60)                                               # 再播 60s
    hist.observe("library://b", "B", "歌手B", "playing", 0, 300)     # 切到 B，结算 A

    rec_a = next(t for t in hist.recent(5) if t["uri"] == "library://a")
    assert rec_a.get("played_sec") == pytest.approx(180, abs=1), (
        "有效播放应只计非暂停的 180s，实际 %r（把暂停也算了）"
        % rec_a.get("played_sec"))
    assert rec_a.get("implicit") == "skip_partial", (
        "180/700 不是完整播放，不应标记 complete")
