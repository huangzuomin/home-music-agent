"""T09 / T22 —— 默认播放器离线时不擅自切换（IMP-03b 已修复）+ 控制目标锁定。

契约（IMP-03b）：pick_player 只解析默认绑定目标（squeezebox-touch）；
离线时如实返回其不可用状态，绝不静默改投其他播放器。
读取、显示、写入使用同一台绑定播放器（控制核心路由亦同）。
"""
from __future__ import annotations

from context import MAClient


def _make(players: list[dict]) -> MAClient:
    mac = MAClient(env_loader=lambda: {})
    fixed_players = players

    def fake_api(command: str, args: dict | None = None):
        if command == "players/all":
            return [dict(p) for p in fixed_players]
        return None

    mac.api = fake_api          # 打桩读取路径；行为等价于真实 HTTP 通道
    return mac


def test_squeezebox_preferred_when_available():
    """正向契约：默认 squeezebox 在线 → 选它。"""
    mac = _make([
        {"player_id": "cast-1", "name": "cast", "available": True,
         "type": "cast"},
        {"player_id": "sq-1", "name": "Squeezebox Touch", "available": True,
         "type": "squeezebox"},
    ])
    assert mac.pick_player().get("player_id") == "sq-1"


def test_t09_t22_default_player_offline__no_silent_fallback():
    """默认播放器离线、另一台在线 → 如实返回默认播放器的不可用状态。"""
    mac = _make([
        {"player_id": "sq-1", "name": "Squeezebox Touch", "available": False,
         "type": "squeezebox"},
        {"player_id": "cast-1", "name": "study-cast", "available": True,
         "type": "cast"},
    ])
    chosen = mac.pick_player()
    assert chosen.get("player_id") == "sq-1", (
        "不得静默切到 %r" % chosen.get("player_id"))
    assert chosen.get("available") is False, "离线状态应如实保留"
    assert "cast" not in chosen.get("player_id", "")


def test_t22_reads_and_writes_target_bound_player():
    """锁定：多次解析的目标都是绑定播放器（不因轮询/换通道漂移）。"""
    mac = _make([
        {"player_id": "sq-1", "name": "Squeezebox Touch", "available": False,
         "type": "squeezebox"},
        {"player_id": "cast-1", "name": "study-cast", "available": True,
         "type": "cast"},
    ])
    for _ in range(3):
        p = mac.pick_player()
        assert p.get("player_id") == "sq-1"


def test_ma2x_type_is_generic_player__match_by_provider():
    """MA 2.x 回归：所有播放器 type 统一为 "player"，厂商挪进 provider
    （2026-09-19 实测 squeezelite: type=player, provider=squeezelite）。
    pick_player 必须在 type/provider/name 里找 squeeze，否则上下文永久为空。
    """
    mac = _make([
        {"player_id": "cast-1", "name": "书房", "available": True,
         "type": "player", "provider": "chromecast"},
        {"player_id": "sq-1", "name": "squeezelite", "available": True,
         "type": "player", "provider": "squeezelite"},
        {"player_id": "web-1", "name": "Web (Chrome on Windows)",
         "available": True, "type": "player", "provider": "sendspin"},
    ])
    assert mac.pick_player().get("player_id") == "sq-1"
