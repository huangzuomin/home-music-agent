"""IMP-06 — 搜索决策与场景就绪度（离线测试，样例数据自造）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, "D:/Work/home-music-agent/services/voice-gateway")

from catalog_resolver import resolve


def test_resolver_no_result_keeps_empty_matches():
    """契约（R13）：查询无结果 → matches 为空，调用方保持当前队列。"""
    r = resolve("不存在的歌", [])
    assert r["matches"] == [] and r["type"] == "track"


def test_resolver_classifies_availability_and_caps_three():
    cands = [{"title": f"晴天 v{i}", "artist": "周杰伦",
              "uri": f"library://{i}", "is_playable": True}
             for i in range(6)]
    r = resolve("晴天", cands)
    assert len(r["matches"]) == 3 and r["ambiguous"] is True
    assert all(m["availability"] == "full" for m in r["matches"])


def test_resolver_mixed_availability():
    cands = [
        {"title": "晴天", "artist": "周杰伦", "uri": "library://31",
         "is_playable": True},
        {"title": "晴天 Live", "artist": "周杰伦", "uri": "qq://1",
         "is_playable": True},
        {"title": "晴天 翻唱", "artist": "某人", "uri": "x://2",
         "is_playable": False},
    ]
    r = resolve("晴天", cands)
    kinds = {m["uri"]: m["availability"] for m in r["matches"]}
    assert kinds["library://31"] == "full"
    assert kinds["qq://1"] == "preview"
    assert kinds["x://2"] == "unavailable"


def test_resolver_detects_entity_type():
    cands = [{"title": "晴天", "artist": "周杰伦", "uri": "library://31"}]
    r = resolve("周杰伦", cands)
    assert r["type"] == "artist"
    r = resolve("晴天", cands)
    assert r["type"] == "track"
