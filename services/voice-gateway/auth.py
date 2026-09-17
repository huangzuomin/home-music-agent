#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-04 — 设备配对、令牌鉴权、Origin 校验与请求限速。

设计（ADR-05）：
    * 管理员签发短期一次性配对码 → 设备换取 (device_id, token)；
    * token 只存 sha256 哈希（devices.token_hash，经 ControlStore 持久化）；
    * 设备令牌可撤销；成员昵称只决定偏好范围，不承担鉴权；
    * 写操作校验 Origin（同源/白名单）；每设备简单限速。

强制开关：DEVICE_AUTH_REQUIRED=false 时鉴权放行（兼容现状/回滚）；
部署切换窗口置 true（cutover 检查单）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class DeviceAuth:
    """设备注册表 + 配对码 + 限速。线程安全；配对码在内存中短期有效。"""

    def __init__(self, store=None, pairing_ttl_sec: float = 600.0,
                 rate_limit: int = 120, rate_window_sec: float = 60.0) -> None:
        self._store = store                    # ControlStore（devices 持久化）
        self._pairing_ttl = float(pairing_ttl_sec)
        self._rate_limit = int(rate_limit)
        self._rate_window = float(rate_window_sec)
        self._lock = threading.Lock()
        self._pairing: dict[str, dict[str, float]] = {}   # code -> {expires}
        self._rate: dict[str, deque] = defaultdict(lambda: deque(maxlen=512))

    # ------------------------------------------------------------ 配对
    def issue_pairing_code(self, admin_key: str | None = None,
                           admin_key_required: bool = False,
                           ttl_sec: float | None = None) -> dict[str, Any]:
        """管理员签发一次性配对码。要求管理密钥时校验失败即拒绝。"""
        if admin_key_required:
            expected = os.environ.get("ADMIN_KEY", "")
            if not expected or not hmac.compare_digest(str(admin_key or ""),
                                                       expected):
                return {"ok": False, "error": "PERMISSION_DENIED"}
        code = "-".join(secrets.token_hex(2).upper() for _ in range(2))
        ttl = float(ttl_sec or self._pairing_ttl)
        with self._lock:
            self._cleanup_codes()
            self._pairing[code] = {"expires": time.time() + ttl}
        return {"ok": True, "code": code, "expires_in_sec": ttl}

    def pair(self, code: str, device_name: str = "",
             kind: str = "pwa") -> dict[str, Any] | None:
        """一次性配对码 → (device_id, token)。失败/过期返回 None。"""
        with self._lock:
            self._cleanup_codes()
            info = self._pairing.pop(code, None)
        if info is None or info["expires"] < time.time():
            return None
        device_id = "dev-" + secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        if self._store is not None:
            self._store.ensure_device(device_id, name=device_name, kind=kind)
            self._store.set_device_token(device_id, _sha256(token))
        return {"device_id": device_id, "token": token,
                "name": device_name, "kind": kind}

    def revoke(self, device_id: str) -> bool:
        if self._store is None:
            return False
        return bool(self._store.revoke_device(device_id))

    # ------------------------------------------------------------ 鉴权
    def authenticate(self, device_id: str | None, token: str | None) -> bool:
        if not device_id or not token:
            return False
        if self._store is None:
            return False
        dev = self._store.get_device(device_id)
        if not dev or dev.get("revoked"):
            return False
        expected = dev.get("token_hash") or ""
        return hmac.compare_digest(expected, _sha256(token))

    def check_rate(self, device_id: str, limit: int | None = None,
                   window: float | None = None) -> bool:
        """滑动窗口限速。返回 True = 允许。"""
        limit = int(limit or self._rate_limit)
        window = float(window or self._rate_window)
        now = time.time()
        dq = self._rate[device_id]
        while dq and dq[0] < now - window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def _cleanup_codes(self) -> None:
        now = time.time()
        for code in [c for c, i in self._pairing.items()
                     if i["expires"] < now]:
            self._pairing.pop(code, None)


# ---------------------------------------------------------------- Origin

def check_origin(origin: str | None, allowed_hosts: list[str]) -> bool:
    """写操作的 Origin 校验：无 Origin（非浏览器/语音客户端）放行；
    有 Origin 时必须命中白名单（精确匹配 host:port 或 scheme://host）。"""
    if not origin:
        return True
    o = origin.rstrip("/")
    for allowed in allowed_hosts:
        a = str(allowed).rstrip("/")
        if o == a or o.endswith("://" + a.split("://", 1)[-1]):
            return True
    return False
