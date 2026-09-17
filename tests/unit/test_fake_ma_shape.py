"""FakeMA 形状与实测基线的一致性守卫。

baseline-manifest.json 的实测事实：
    * players/all 条目**不含** active / queue_id 字段
    * MA HTTP 面仅 api/auth/info/setup（命令面走 WS）
Fake 层必须编码这些事实，否则测试会按官方文档臆造接口。
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.fakes.fake_ma import FakeMA


def test_fake_ma_players_shape_matches_measured_baseline():
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" /
         "ma_players_sample.json").read_text(encoding="utf-8"))
    fake = FakeMA()
    # FakeMA 的默认播放器集与实测样例一一对应（同 id 同字段子集）
    fake_ids = {p["player_id"] for p in fake.players}
    sample_ids = {p["player_id"] for p in fixture}
    assert fake_ids == sample_ids
    for p in fake.players:
        assert "active" not in p, "实测部署版 players 响应不含 active 字段"
        assert "queue_id" not in p, "实测部署版 players 响应不含 queue_id 字段"


def test_fake_ma_command_surface_is_websocket_only():
    """HTTP /api-docs 面仅 api/auth/info/setup；命令面（players/queues/search）
    只应通过 POST /api/call（= WS 命令的 HTTP 等价通道）触达。"""
    fake = FakeMA()
    fake.dispatch("players/all")
    fake.dispatch("music/search", {"search_query": "晴天"})
    cmds = [c for c, _ in fake.calls]
    assert "players/all" in cmds and "music/search" in cmds
    assert not any(c.startswith("/") for c in cmds), (
        "命令面不是 REST 路径——不得把 OpenAPI 的 HTTP 路径当成 MA 命令")
