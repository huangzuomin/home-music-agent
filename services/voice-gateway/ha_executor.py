#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-03c — HA 执行适配：唯一的 MA 写动作下发通路（D13/ADR-01/ADR-03）。

所有系统管理的 MA 写操作经 HA 短执行脚本 ``script.music_execute_v1`` 下发：
    * HA 只接收**已解析的短动作**（ma_command + 参数 JSON + command_id）；
    * 慢搜索/模型推理不进入本通路（T10：模型超时不阻塞暂停）；
    * 执行回执（HA 受理）与播放器确认分离——确认由 playback_state 观测。
"""
from __future__ import annotations

import json
from typing import Any, Callable

SCRIPT_NAME = "music_execute_v1"

# 动作 → MA 队列命令（队列类：需要 queue_id）
QUEUE_COMMANDS: dict[str, str] = {
    "pause": "player_queues/play_pause",
    "resume": "player_queues/play",
    "stop": "player_queues/stop",
    "next": "player_queues/next",
    "previous": "player_queues/previous",
}


class HAExecutor:
    """把规范化动作翻译成 MA 命令并经 HA script 下发。"""

    def __init__(self, ha: Any,
                 queue_resolver: Callable[[str], dict] | None = None) -> None:
        """ha：具备 call_script(name, variables) 的对象（生产=HAClient）。

        queue_resolver(player_id) → {"queue_id": ...}：队列类动作在 args
        未显式给出 queue_id 时，由它按绑定播放器解析活动队列。
        """
        self.ha = ha
        self.queue_resolver = queue_resolver
        self.executions: list[dict[str, Any]] = []

    def execute(self, action: str, args: dict[str, Any] | None,
                player_id: str, command_id: str) -> dict[str, Any]:
        args = dict(args or {})
        if action == "play_now":
            # 立即播放：用 uri 替换队列（MA play_media 语义）
            uri = str(args.get("uri") or "")
            if not uri:
                rec = {"executed": False, "error": "NO_MEDIA",
                       "ma_command": None, "ma_args": {}}
                self.executions.append(rec)
                return rec
            queue_id = str(args.get("queue_id") or "")
            if not queue_id and self.queue_resolver:
                q = self.queue_resolver(player_id) or {}
                queue_id = str(q.get("queue_id") or "")
            if not queue_id:
                rec = {"executed": False, "error": "NO_QUEUE",
                       "ma_command": None, "ma_args": {}}
                self.executions.append(rec)
                return rec
            ma_command = "player_queues/play_media"
            ma_args = {"queue_id": queue_id, "media": uri}
            receipt = self.ha.call_script(SCRIPT_NAME, {
                "ma_command": ma_command,
                "ma_args_json": json.dumps(ma_args, ensure_ascii=False),
                "command_id": command_id})
            rec = {"executed": bool(receipt.get("ok")),
                   "ma_command": ma_command, "ma_args": ma_args,
                   "receipt": receipt}
            self.executions.append(rec)
            return rec
        if action in QUEUE_COMMANDS:
            queue_id = str(args.get("queue_id") or "")
            if not queue_id and self.queue_resolver:
                q = self.queue_resolver(player_id) or {}
                queue_id = str(q.get("queue_id") or "")
            if not queue_id:
                rec = {"executed": False, "error": "NO_QUEUE",
                       "ma_command": None, "ma_args": {}}
                self.executions.append(rec)
                return rec
            ma_command = QUEUE_COMMANDS[action]
            ma_args: dict[str, Any] = {"queue_id": queue_id}
        elif action == "volume_set":
            ma_command = "players/cmd/volume_set"
            ma_args = {"player_id": player_id,
                       "volume_level": int(args.get("level", 0))}
        else:
            rec = {"executed": False, "error": f"unsupported action {action}",
                   "ma_command": None, "ma_args": {}}
            self.executions.append(rec)
            return rec

        receipt = self.ha.call_script(SCRIPT_NAME, {
            "ma_command": ma_command,
            "ma_args_json": json.dumps(ma_args, ensure_ascii=False),
            "command_id": command_id,
        })
        ok = bool(receipt.get("ok"))
        rec = {"executed": ok,
               "ma_command": ma_command, "ma_args": ma_args,
               "receipt": receipt}
        self.executions.append(rec)
        return rec
