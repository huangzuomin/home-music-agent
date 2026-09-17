#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-07 — /api/v1 版本化业务 API（PWA 同源入口，计划 §4.5 最小形状）。

职责边界（ADR-01/03）：
    * 所有写操作经 CommandCoordinator（幂等/intent_epoch/STOP 屏障）；
    * 快照只读 MA（playback_state），带 freshness，不存第二份 now_playing；
    * 设备鉴权（DEVICE_AUTH_REQUIRED=true 时强制；cutover 窗口启用）；
    * 慢搜索不在命令执行路径（T10：模型/搜索超时不阻塞暂停）。

依赖通过 init_deps 注入（由 app.py 装配），避免循环导入。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1")

_deps: dict[str, Any] = {}


def init_deps(**deps: Any) -> None:
    """由 app.py 启动时注入：
    coordinator / store / auth / snapshot / scenes / search / feedback /
    settings(dict: device_auth_required, allowed_origins, admin_key)。"""
    _deps.clear()
    _deps.update(deps)


def _auth_device(request: Request) -> str | None:
    """设备鉴权：未启用时返回 "local"；启用时校验 X-Device-ID/-Token。"""
    if not _deps.get("device_auth_required"):
        return "local"
    auth = _dep("auth")
    device_id = request.headers.get("X-Device-ID", "")
    token = request.headers.get("X-Device-Token", "")
    if not auth or not auth.authenticate(device_id, token):
        return None
    if not auth.check_rate(device_id):
        return None
    return device_id


def _origin_ok(request: Request) -> bool:
    check = _dep("check_origin")
    return (not check) or check(request.headers.get("Origin"))


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "DEVICE_AUTH_REQUIRED"}, status_code=401)


def _forbidden() -> JSONResponse:
    return JSONResponse({"error": "PERMISSION_DENIED"}, status_code=403)


# ---------------------------------------------------------------- 配对

class PairReq(BaseModel):
    code: str
    device_name: str = ""
    kind: str = "pwa"


@router.post("/pair")
def pair(body: PairReq):
    auth = _dep("auth")
    res = auth.pair(body.code, device_name=body.device_name, kind=body.kind)
    if res is None:
        return JSONResponse({"error": "INVALID_OR_EXPIRED_CODE"}, status_code=403)
    return res


# ---------------------------------------------------------------- 命令

class CommandReq(BaseModel):
    request_id: str
    action: str
    args: dict = {}
    player_id: str = ""
    source: str = "pwa"


@router.post("/commands")
def post_command(body: CommandReq, request: Request):
    device_id = _auth_device(request)
    if device_id is None:
        return _unauthorized()
    if not _origin_ok(request):
        return JSONResponse({"error": "ORIGIN_NOT_ALLOWED"}, status_code=403)
    coordinator = _dep("coordinator")
    from contracts import CommandRequest
    req = CommandRequest(
        device_id=device_id, request_id=body.request_id, action=body.action,
        args=body.args, player_id=body.player_id, source=body.source)
    res = coordinator.submit(req)
    if res.get("status") == "error":
        return JSONResponse(res, status_code=409)
    return res


@router.get("/commands/{command_id}")
def get_command(command_id: str, request: Request):
    device_id = _auth_device(request)
    if device_id is None:
        return _unauthorized()
    store = _dep("store")
    row = store.get_command_by_id(command_id)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if row.get("device_id") != device_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    return row


# ---------------------------------------------------------------- 快照

@router.get("/snapshot")
def snapshot(request: Request):
    device_id = _auth_device(request)
    if device_id is None:
        return _unauthorized()
    snap_fn = _dep("snapshot")
    return snap_fn()


# ---------------------------------------------------------------- 场景

@router.get("/scenes")
def scenes():
    return {"scenes": _dep("scenes")()}


# ---------------------------------------------------------------- 搜索

@router.get("/search")
def search(q: str, request: Request = None):
    device_id = _auth_device(request)
    if device_id is None:
        return _unauthorized()
    search_fn = _dep("search")
    if not search_fn:
        return JSONResponse({"error": "search unavailable"}, status_code=503)
    return search_fn(q)
