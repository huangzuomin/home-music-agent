"""T12 —— 先赞 A 再赞 B，各计一次；累计快照重复计分被消除（xfail 复现）。

对应 review：G04。根因（源码级）：
SessionStore.add_feedback 每次追加的 patch 里携带**全量 feedback 列表**，
而 taste.build_profile 逐 patch 累加列表中的每一项 → 第 N 次反馈会在
前 N-1 个 patch 中被重复计入（第 1 个赞实际被计 2 次、第 2 个 1 次……）。
修复归属 IMP-02（读取侧去重）/ IMP-09（feedback 事件化）。
"""
from __future__ import annotations

import pytest

from session import SessionStore


@pytest.mark.xfail(strict=True, reason="G04/T12：累计快照重复计分，修复在 IMP-02/09")
def test_t12_like_a_then_b_counted_once_each(monkeypatch, tmp_path):
    import time

    import taste as taste_mod

    # ---- 用真实 SessionStore 生成与生产一致的 jsonl ----
    sess_file = tmp_path / "agent-sessions.jsonl"
    store = SessionStore(path=sess_file)
    sess, _ = store.get("auto", "u1")
    store.add_feedback(sess, "strong_positive",
                       target={"title": "A", "artist": "歌手A",
                               "uri": "library://a"})
    store.add_feedback(sess, "strong_positive",
                       target={"title": "B", "artist": "歌手B",
                               "uri": "library://b"})

    # ---- taste 在同一份快照上聚合 ----
    monkeypatch.setattr(taste_mod, "AGENT_SESSIONS", sess_file)
    monkeypatch.setattr(taste_mod, "TRACK_HISTORY", tmp_path / "none.jsonl")
    monkeypatch.setattr(taste_mod, "FETCHER_JOBS", tmp_path / "none-jobs.jsonl")

    profile = taste_mod.build_profile(tracks=[], now=time.time())

    counts = profile.get("signal_counts", {})
    assert counts.get("strong_positive") == 2, (
        "两次点赞应各计一次（快照重复不得重复计分），实际 %r"
        % counts.get("strong_positive"))
    weights = {a["name"]: a["weight"] for a in profile.get("artists", [])}
    assert weights.get("歌手A") == pytest.approx(10.0, abs=0.5), (
        "歌手A 只应获得一次满分，实际 %r" % weights.get("歌手A"))
    assert weights.get("歌手B") == pytest.approx(10.0, abs=0.5), (
        "歌手B 只应获得一次满分，实际 %r" % weights.get("歌手B"))
