#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-03c — CommandCoordinator：意图代际、幂等、串行裁决与 STOP 屏障。

契约（计划 §4，ADR-03）：
    * 幂等：(device_id, request_id) 唯一约束。同 ID 同内容 → 返回原命令；
      同 ID 不同内容 → REQUEST_CONFLICT；非幂等动作结果未知时不盲重试。
    * STOP 屏障：先使未派发/未到终态的旧计划失效（superseded + epoch 推进），
      再执行停止；在途最小写动作不虚构取消——由对账标 unknown。
    * 来源裁决：backfill_callback/automation 等自动源不得触发播放类动作
      （IMP-02 阻断的延伸）。
    * 慢工作（搜索/模型推理）不进入本路径（T10）。

确认边界：executor 回执 = HA 受理；``player_confirmed`` 需要快照观测——
默认不声称确认（False），快照确认器由部署注入（IMP-08 完善）。
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

import contracts
from contracts import (
    ACTION_STOP,
    ERR_PERMISSION_DENIED,
    ERR_REQUEST_CONFLICT,
    ERR_CAPABILITY_UNSUPPORTED,
    STATUS_ACCEPTED,
    STATUS_EXECUTING,
    STATUS_FAILED,
    STATUS_PLAYER_CONFIRMED,
    STATUS_QUEUED,
    STATUS_SUPERSEDED,
    STATUS_UNKNOWN,
    CommandRequest,
)

AUTO_SOURCES = frozenset({"backfill_callback", "automation", "legacy"})
PLAY_ACTIONS = frozenset({"play_now", "enqueue_next", "enqueue_last"})


class CommandCoordinator:
    def __init__(self, store, executor,
                 default_player: dict[str, str] | None = None,
                 boot_id: str | None = None) -> None:
        self.store = store
        self.executor = executor
        self.boot_id = boot_id or uuid.uuid4().hex
        self.intent_epoch = int(store.get_meta("intent_epoch", "1"))
        self.default_player = default_player or {
            "player_id": "squeezebox-touch", "player_name": "Squeezebox Touch"}
        self._pending: list[tuple[str, CommandRequest]] = []
        # 启动对账（X02 前提）：上次进程遗留的 in-flight 命令标 unknown。
        self.store.reconcile_inflight()

    # ------------------------------------------------------------ 提交
    def submit(self, req: CommandRequest, defer: bool = False) -> dict[str, Any]:
        """登记并执行命令（defer=True 时仅入未派发队列，用于 STOP 屏障测试）。"""
        if req.action not in contracts.ACTIONS:
            self.store.record_command(req.device_id, req.request_id,
                                      "__unsupported__", {}, status="failed")
            return {"status": "error", "error": "CAPABILITY_UNSUPPORTED",
                    "command_id": None}

        rec = self.store.record_command(
            req.device_id, req.request_id, req.action, req.args,
            player_id=req.player_id, intent_epoch=req.intent_epoch)

        if not rec["created"]:
            if rec["conflict"]:
                # X05：同 request_id 不同内容 → 冲突，不执行
                return {"status": "error", "error": "REQUEST_CONFLICT",
                        "command_id": rec["command_id"], "conflict": True}
            # 幂等重放：返回原命令状态，不重复执行（T11）
            existing = self.store.get_command(req.device_id, req.request_id)
            return {"status": "ok", "deduplicated": True,
                    "command_id": rec["command_id"],
                    "intent_epoch": existing["intent_epoch"],
                    "action": existing["action"],
                    "result": existing["result"]}

        cid = rec["command_id"]

        # 来源裁决（X03 延伸）：自动源不得触发播放类动作（IMP-02 阻断的延伸）
        if req.source in AUTO_SOURCES and req.action in PLAY_ACTIONS:
            self.store.update_command_status(
                cid, "failed", {"error": "PERMISSION_DENIED",
                                "reason": "autoplay blocked (IMP-02)"})
            return {"status": "error", "error": "PERMISSION_DENIED",
                    "command_id": cid}

        target = ({"player_id": req.player_id} if req.player_id
                  else dict(self.default_player))
        pid = target.get("player_id") or ""

        if req.action == ACTION_STOP:
            return self._execute_stop(cid, req, pid, defer)

        if defer:
            self._pending.append((cid, req))
            self.store.update_command_status(cid, STATUS_QUEUED)
            return {"status": "queued(deferred)", "command_id": cid}

        return self._deliver(cid, req, pid)

    # ------------------------------------------------------------ STOP 屏障
    def _execute_stop(self, cid: str, req: CommandRequest,
                      pid: str, defer: bool) -> dict[str, Any]:
        """STOP 屏障（X01）：先失效未派发旧计划与旧 epoch，再执行停止。

        已派发的最小写动作不能虚构取消——它们或自然结束，或由对账标
        unknown；停止动作本身排在已派发动作之后同步执行。
        """
        superseded = self.store.supersede_pending(self.intent_epoch)
        dropped = [c for c, _ in self._pending]
        self._pending.clear()
        self.intent_epoch += 1                     # 推进播放意图代际
        self.store.set_meta("intent_epoch", str(self.intent_epoch))

        if defer:
            self.store.update_command_status(cid, STATUS_EXECUTING,
                                             {"player_confirmed": False})
            return {"status": "ok", "intent": "stop", "command_id": cid,
                    "superseded": superseded, "dropped_pending": dropped}

        self.store.update_command_status(cid, STATUS_EXECUTING)
        receipt = self.executor.execute(ACTION_STOP, {}, pid, cid)
        if receipt.get("executed"):
            self.store.update_command_status(cid, STATUS_PLAYER_CONFIRMED,
                                             {"player_confirmed": True})
            return {"status": "ok", "intent": "stop", "command_id": cid,
                    "player_confirmed": True,
                    "superseded": superseded, "dropped_pending": dropped}
        err = receipt.get("error") or "execution failed"
        self.store.update_command_status(cid, STATUS_UNKNOWN,
                                         {"error": err})
        return {"status": "error", "intent": "stop", "command_id": cid,
                "error": f"{err}（停止结果未知，请查看播放器）"}

    # ------------------------------------------------------------ 派发
    def _deliver(self, cid: str, req: CommandRequest, pid: str) -> dict[str, Any]:
        self.store.update_command_status(cid, STATUS_EXECUTING)
        receipt = self.executor.execute(req.action, req.args, pid, cid)
        if not receipt.get("executed"):
            self.store.update_command_status(cid, STATUS_FAILED,
                                             {"error": receipt.get("error")})
            return {"status": "error", "error": receipt.get("error"),
                    "command_id": cid}
        # 回执 = HA 受理。player_confirmed 需要快照观测（IMP-08 接入
        # playback_state 确认器）；在此之前如实保持 False。
        self.store.update_command_status(cid, STATUS_EXECUTING,
                                         {"player_confirmed": False})
        return {"status": "ok", "command_id": cid, "intent": req.action,
                "player_confirmed": False}

    # ------------------------------------------------------------ 工具
    def flush_pending_superseded(self) -> int:
        n = 0
        for cid, _req in self._pending:
            self.store.update_command_status(cid, STATUS_SUPERSEDED)
            n += 1
        self._pending.clear()
        return n
