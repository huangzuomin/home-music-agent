"""T09 / T22 —— 默认播放器离线时不擅自切换（IMP-03b 已修复，原 xfail 翻绿）。

契约（IMP-03b）：pick_player 只解析默认绑定目标（squeezebox-touch）；
离线时如实返回其不可用状态，绝不静默改投其他播放器。
读取、显示、写入使用同一台绑定播放器。
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


def test_t22_commands_go_only_to_bound_player(vg=None):
    """控制动作的目标脚本集合固定——不存在第二台播放器的调用路径。"""
    mac = _make([
        {"player_id": "sq-1", "name": "Squeezebox Touch", "available": False,
         "type": "squeezebox"},
        {"player_id": "cast-1", "name": "study-cast", "available": True,
         "type": "cast"},
    ])
    # MAClient 无写方法（D13）；此处锁定读取目标也只指向绑定播放器
    for _ in range(3):
        p = mac.pick_player()
        assert p.get("player_id") == "sq-1"
