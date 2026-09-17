#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-03a — 统一契约：命令生命周期、动作集、错误码、版本号边界。

本模块只定义**契约常量与纯数据结构**，不做任何 IO。
设计依据：docs/implementation 计划 §4（控制核心契约）与 ADR-01/02。

三种版本号不得混用（ADR-02）：
    intent_epoch   播放意图代际：新的立即替换/停止/会话结束/显式取消使其
                   递增，使相关旧计划失效；普通时间流逝不改变它。
    queue_revision 队列内容/顺序/游标的语义修订号：供未来队列调整与撤销
                   （IMP-10 起）使用；播放器的周期性刷新不算修订。
    event_seq + server_boot_id
                   前端推送游标：SSE 断流重连对账用，不能当播放授权。
    另有 timer_revision（定时结束，IMP-11）：不受音量调整影响。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 命令生命周期

STATUS_ACCEPTED = "accepted"
STATUS_RESOLVING = "resolving"
STATUS_QUEUED = "queued"
STATUS_EXECUTING = "executing"
STATUS_PLAYER_CONFIRMED = "player_confirmed"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"
STATUS_UNKNOWN = "unknown"

COMMAND_STATUSES = frozenset({
    STATUS_ACCEPTED, STATUS_RESOLVING, STATUS_QUEUED, STATUS_EXECUTING,
    STATUS_PLAYER_CONFIRMED, STATUS_FAILED, STATUS_SUPERSEDED, STATUS_UNKNOWN,
})

# 终态：到达后命令不再变化（unknown 由对账流程负责收敛或标注）。
TERMINAL_STATUSES = frozenset({
    STATUS_PLAYER_CONFIRMED, STATUS_FAILED, STATUS_SUPERSEDED, STATUS_UNKNOWN,
})

# ---------------------------------------------------------------- 动作集

ACTION_PLAY_NOW = "play_now"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_STOP = "stop"
ACTION_NEXT = "next"
ACTION_PREVIOUS = "previous"
ACTION_VOLUME_SET = "volume_set"
ACTION_MUSIC_MUTE = "music_mute"
ACTION_MUSIC_UNMUTE = "music_unmute"
ACTION_SCENE_START = "scene_start"
ACTION_ENQUEUE_NEXT = "enqueue_next"
ACTION_ENQUEUE_LAST = "enqueue_last"

ACTIONS = frozenset({
    ACTION_PLAY_NOW, ACTION_PAUSE, ACTION_RESUME, ACTION_STOP,
    ACTION_NEXT, ACTION_PREVIOUS, ACTION_VOLUME_SET,
    ACTION_MUSIC_MUTE, ACTION_MUSIC_UNMUTE,
    ACTION_SCENE_START, ACTION_ENQUEUE_NEXT, ACTION_ENQUEUE_LAST,
})

# next/previous 依赖播放器当时的队列游标——结果未知时不得盲重试。
NON_IDEMPOTENT_ACTIONS = frozenset({ACTION_NEXT, ACTION_PREVIOUS})


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


# ---------------------------------------------------------------- 错误码

ERR_PLAYER_OFFLINE = "PLAYER_OFFLINE"
ERR_TARGET_NOT_BOUND = "TARGET_NOT_BOUND"
ERR_STATE_STALE = "STATE_STALE"
ERR_NO_MATCH = "NO_MATCH"
ERR_AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
ERR_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
ERR_SOURCE_PREVIEW_ONLY = "SOURCE_PREVIEW_ONLY"
ERR_CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
ERR_LLM_TIMEOUT = "LLM_TIMEOUT"
ERR_STT_UNAVAILABLE = "STT_UNAVAILABLE"
ERR_EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
ERR_REVISION_CONFLICT = "REVISION_CONFLICT"
ERR_PERMISSION_DENIED = "PERMISSION_DENIED"
ERR_MIC_CONTROL_UNSUPPORTED = "MIC_CONTROL_UNSUPPORTED"
ERR_REQUEST_CONFLICT = "REQUEST_CONFLICT"

ERROR_HINTS: dict[str, str] = {
    ERR_PLAYER_OFFLINE: "目标音箱当前离线，请检查电源与网络",
    ERR_TARGET_NOT_BOUND: "还没有为这台设备绑定播放器",
    ERR_STATE_STALE: "播放状态已过期，正在重新获取",
    ERR_NO_MATCH: "没有找到匹配的内容",
    ERR_AMBIGUOUS_MATCH: "找到多个可能的结果，请说得更具体些",
    ERR_SOURCE_UNAVAILABLE: "音源暂时不可用",
    ERR_SOURCE_PREVIEW_ONLY: "该内容只有试听版本",
    ERR_CAPABILITY_UNSUPPORTED: "当前部署版本不支持这个操作",
    ERR_LLM_TIMEOUT: "理解指令超时，请重试",
    ERR_STT_UNAVAILABLE: "语音识别服务暂时不可用",
    ERR_EXECUTION_UNKNOWN: "指令执行结果未知，请查看播放器状态",
    ERR_REVISION_CONFLICT: "指令与当前状态冲突，已忽略",
    ERR_PERMISSION_DENIED: "没有执行该操作的权限",
    ERR_MIC_CONTROL_UNSUPPORTED: "麦克风开关请使用设备上的关闭键",
    ERR_REQUEST_CONFLICT: "同一请求编号携带了不同内容，已拒绝",
}


# ---------------------------------------------------------------- 数据结构

@dataclass
class VersionContext:
    """一次快照/命令携带的版本上下文（三者边界见 ADR-02）。"""
    intent_epoch: int = 1
    queue_revision: int = 0
    event_seq: int = 0
    server_boot_id: str = ""


@dataclass
class CommandRequest:
    """一条控制命令的规范化输入。

    幂等契约（计划 §4.3）：服务端以 (device_id, request_id) 建唯一约束。
    相同 ID + 相同内容 → 返回同一 command；相同 ID + 不同内容 → 冲突。
    """
    device_id: str
    request_id: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    intent_epoch: int = 1
    player_id: str = ""
    listening_session_id: str = ""
    source: str = "api"

    def content_fingerprint(self) -> str:
        """参与幂等判定的内容指纹（不含 source/时间等元数据）。"""
        import json
        payload = json.dumps(
            {"action": self.action, "args": self.args, "player_id": self.player_id},
            ensure_ascii=False, sort_keys=True)
        import hashlib
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CommandRecord:
    command_id: str
    device_id: str
    request_id: str
    action: str
    args: dict[str, Any]
    intent_epoch: int
    status: str
    player_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
