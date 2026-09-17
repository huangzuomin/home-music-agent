"""T09 / T22 —— 默认播放器离线时不擅自切换（当前为缺陷，xfail 复现）+ 优先级锁定。

对应 review：G07。根因（源码级）：
context.MAClient.pick_player 在默认的 Squeezebox 不可用时「回退到任何
available 播放器」（squeezebox 优先 → 任意 available 兜底）。
计划 IMP-03b 明确要求删除该回退：读取、显示与写入必须是同一台绑定播放器。
"""
from __future__ import annotations

import pytest

from context import MAClient


def _make(players: list[dict]) -> MAClient:
    mac = MAClient(env_loader=lambda: {})
    monkeypatch_players = players
    mac._api_stub = monkeypatch_players

    def fake_api(command: str, args: dict | None = None):
        if command == "players/all":
            return monkeypatch_players
        return None

    mac.api = fake_api          # 类型虽变，行为等价：只打桩读取路径
    return mac


def test_squeezebox_preferred_when_available(monkeypatch):
    """正向契约：默认 squeezebox 在线 → 选它（锁定现有优先级逻辑）。"""
    mac = _make([
        {"player_id": "cast-1", "name": "cast", "available": True,
         "type": "cast"},
        {"player_id": "sq-1", "name": "Squeezebox Touch", "available": True,
         "type": "squeezebox"},
    ])
    assert mac.pick_player().get("player_id") == "sq-1"


@pytest.mark.xfail(strict=True, reason="G07/T09/T22：默认播放器离线时擅自回退到任意可用设备，删除回退在 IMP-03b")
def test_t09_t22_default_player_offline__no_silent_fallback(monkeypatch):
    """默认播放器离线、另一台在线 → 不得静默改投另一台。

    契约（IMP-03b）：读取/显示/写入使用同一台**绑定**播放器；目标离线时
    显式反映离线状态并交由用户处理，而不是悄悄换房间。
    """
    mac = _make([
        {"player_id": "sq-1", "name": "Squeezebox Touch", "available": False,
         "type": "squeezebox"},
        {"player_id": "cast-1", "name": "study-cast", "available": True,
         "type": "cast"},
    ])
    chosen = mac.pick_player()
    assert chosen.get("player_id") in ("", None, "sq-1"), (
        "默认播放器离线时不得静默切到 %r" % chosen.get("player_id"))
