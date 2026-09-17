#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-03a — 设备身份与播放器绑定。

设备身份（ADR-05 草案，IMP-04 完善）：
    * 设备在受控入口完成配对后，由服务端签发 device_id；
    * device_id 与 request_id 一起构成命令幂等约束的一半；
    * 成员昵称只决定偏好范围，不承担鉴权。

播放器绑定：
    * scope='default' 为家庭默认播放器（如 squeezebox-touch）；
    * 读取/显示/写入使用同一绑定，默认播放器离线时**不回退**到其他设备
      （T09/T22）——回退删除是 IMP-03b 的验收项。
"""
from __future__ import annotations

from typing import Any

import contracts

DEFAULT_BINDING_SCOPE = "default"
DEFAULT_PLAYER_ID = "squeezebox-touch"   # 计划指定：优先绑定 squeezebox-touch
DEFAULT_PLAYER_NAME = "Squeezebox Touch"


def get_target_player(store, scope: str = DEFAULT_BINDING_SCOPE) -> dict[str, str]:
    """解析命令目标播放器：绑定表优先；无绑定时返回计划默认值。

    返回 {"player_id", "player_name", "bound"} —— bound=False 表示使用的是
    计划默认值（未显式绑定），此时若是「写入类动作」建议先完成配对/绑定。
    """
    row = store.get_binding(scope)
    if row:
        return {"player_id": row["player_id"],
                "player_name": row["player_name"], "bound": True}
    return {"player_id": DEFAULT_PLAYER_ID,
            "player_name": DEFAULT_PLAYER_NAME, "bound": False}


def set_default_player(store, player_id: str, player_name: str = "") -> None:
    store.set_binding(DEFAULT_BINDING_SCOPE, player_id, player_name)


def ensure_device(store, device_id: str, name: str = "",
                  kind: str = "api") -> None:
    store.ensure_device(device_id, name=name, kind=kind)


def validate_action(action: str) -> None:
    if action not in contracts.ACTIONS:
        raise ValueError(
            "unsupported action %r (supported: %s)"
            % (action, ", ".join(sorted(contracts.ACTIONS))))
