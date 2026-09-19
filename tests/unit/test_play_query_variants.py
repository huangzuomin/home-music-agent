"""B3 —— 点播变体解析（MA 本地索引吃不下「歌手+的+歌名」连写）。

实测（2026-09-19，MA 2.10.3）：
  music/search("周杰伦的晴天") → 只有 qqmusic-- 在线条目（60s 试听陷阱）；
  music/search("晴天")        → 本地曲 library://track/36 排第一。
所以点播进控制核心前，先按变体搜本地库，命中即改写 query。
"""
from __future__ import annotations

import app as vg_app


# ---------------------------------------------------------------- 变体派生

def test_variants_artist_de_title():
    assert vg_app._search_variants("周杰伦的晴天") == [
        "周杰伦的晴天", "晴天"]


def test_variants_space_separated():
    assert vg_app._search_variants("周杰伦 晴天") == [
        "周杰伦 晴天", "晴天"]


def test_variants_plain_query_has_no_extra():
    assert vg_app._search_variants("晴天") == ["晴天"]


def test_variants_empty_query():
    assert vg_app._search_variants("") == []


# ---------------------------------------------------------------- 本地解析

class _FakeMA:
    """按 query 返回预设结果的最小 MA 桩。"""

    def __init__(self, by_query: dict[str, list[dict]]):
        self.by_query = by_query
        self.calls: list[str] = []

    def search(self, query: str, limit: int = 5) -> list[dict]:
        self.calls.append(query)
        return self.by_query.get(query, [])


def test_resolve_rewrites_artist_prefixed_query_to_local_hit():
    ma = _FakeMA({
        "周杰伦的晴天": [  # 原词只有 QQ 在线（本地索引不认连写）
            {"name": "晴天", "uri": "qqmusic--x://track/a1", "is_playable": True},
        ],
        "晴天": [          # 变体命中本地曲，且排在第一
            {"name": "晴天", "uri": "library://track/36", "is_playable": True},
            {"name": "晴天", "uri": "qqmusic--x://track/b1", "is_playable": True},
        ],
    })
    resolved = vg_app._resolve_local_query(ma, "周杰伦的晴天")
    assert resolved is not None
    variant, track = resolved
    assert variant == "晴天"
    assert track["uri"] == "library://track/36"


def test_resolve_keeps_original_query_when_it_hits_local():
    """原词直接命中本地 → 不改写（variant == 原词）。"""
    ma = _FakeMA({
        "稻香": [{"name": "稻香", "uri": "library://track/12",
                  "is_playable": True}],
    })
    resolved = vg_app._resolve_local_query(ma, "稻香")
    assert resolved is not None
    assert resolved[0] == "稻香"


def test_resolve_returns_none_when_no_local_anywhere():
    """全变体都只有在线 → None，走既有在线垫播 + 补库流程。"""
    ma = _FakeMA({
        "北斗星的爱": [{"name": "北斗星的爱",
                        "uri": "qqmusic--x://track/c1", "is_playable": True}],
    })
    assert vg_app._resolve_local_query(ma, "北北北") is None
    assert ma.calls == ["北北北"]      # 原词搜过一次即止，无变体可派生


def test_resolve_search_failure_is_safe():
    """MA 查询抛异常 → 视作未命中，绝不能把点播流程炸掉。"""
    class _Boom:
        def search(self, query, limit=5):
            raise RuntimeError("ma down")

    assert vg_app._resolve_local_query(_Boom(), "周杰伦的晴天") is None


def test_reply_for_play_local_hit_announces_full_version():
    ma = _FakeMA({})
    resolved = ("晴天", {"name": "晴天", "uri": "library://track/36"})
    reply = vg_app._reply_for_play(ma, {}, "周杰伦的晴天", resolved=resolved)
    assert "晴天" in reply
    assert "完整版" in reply
