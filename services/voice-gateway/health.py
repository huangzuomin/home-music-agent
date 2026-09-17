#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-04 — 健康检查：liveness 与 readiness 分离。

* /livez：进程存活即 ok（不给依赖状态）。
* /readyz：分组件上报——
    db_writable        控制库可写（IMP-03a SQLite）
    ha_reachable       HA REST 可达（带令牌探测 /api/）
    ma_reachable       MA players/all 可调
    target_player_online  绑定播放器 available
    stt_ready          STT /health 可达
    nas_mount          NFS 挂载来源/标识匹配（不只看目录可写）

失败策略（计划 IMP-04）：LLM/STT/补库故障不使基本播放控制不可用；
NAS 未挂载时禁止入库并显式告警（X08）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


def _http_ok(url: str, token: str = "", timeout: float = 4.0) -> tuple[bool, str]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return (400 <= e.code < 500 and e.code != 401), f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def nas_mount_identity(expected_source: str, mount_point: str) -> dict[str, Any]:
    """校验挂载来源与标识：/proc/mounts 中须出现 expected_source 与挂载点。

    只看「目录存在且可写」会命中宿主机同名空目录（X08）——这里以
    内核挂载表为准。
    """
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "detail": f"无法读取 /proc/mounts: {e}"}
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == mount_point:
            src = parts[0]
            ok = (not expected_source) or (expected_source in src)
            return {"ok": ok, "source": src, "mount": mount_point}
    return {"ok": False, "detail": f"{mount_point} 未挂载"}


class HealthChecker:
    def __init__(self,
                 db_path: str,
                 ha_base: str, ha_token: str,
                 ma_probe: Callable[[], bool],
                 target_player_available: Callable[[], bool],
                 stt_base: str,
                 nas_expected_source: str = "", nas_mount_point: str = "/mnt/music",
                 control_db_probe: Callable[[], bool] | None = None,
                 ha_probe: Callable[[], tuple[bool, str]] | None = None,
                 stt_probe: Callable[[], tuple[bool, str]] | None = None) -> None:
        self.db_path = db_path
        self.ha_base = ha_base
        self.ha_token = ha_token
        self._ma_probe = ma_probe                 # () -> bool
        self._player_available = target_player_available  # () -> bool
        self.stt_base = stt_base
        self.nas_expected_source = nas_expected_source
        self.nas_mount_point = nas_mount_point
        self._control_db_probe = control_db_probe or (lambda: True)
        # HTTP 探针可注入（测试/特殊部署）；默认真实 HTTP。
        self._ha_probe_fn = ha_probe
        self._stt_probe_fn = stt_probe

    def liveness(self) -> dict[str, Any]:
        return {"ok": True, "component": "liveness"}

    def readiness(self) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}

        # db 可写（控制核心前提）
        try:
            checks["db_writable"] = {"ok": bool(self._control_db_probe())}
        except Exception as e:
            checks["db_writable"] = {"ok": False, "detail": str(e)[:80]}

        # HA 可达
        if self._ha_probe_fn:
            ok, detail = self._ha_probe_fn()
        else:
            ok, detail = _http_ok(self.ha_base + "/api/", self.ha_token)
        checks["ha_reachable"] = {"ok": ok, "detail": detail}

        # MA 可达
        try:
            checks["ma_reachable"] = {"ok": bool(self._ma_probe())}
        except Exception as e:
            checks["ma_reachable"] = {"ok": False, "detail": str(e)[:80]}

        # 目标播放器在线
        try:
            checks["target_player_online"] = {
                "ok": bool(self._player_available())}
        except Exception as e:
            checks["target_player_online"] = {"ok": False,
                                              "detail": str(e)[:80]}

        # STT 就绪（基本播放控制不依赖，但语音入口依赖 → 单列不并入 ready）
        if self._stt_probe_fn:
            ok, detail = self._stt_probe_fn()
        else:
            ok, detail = _http_ok(self.stt_base.rstrip("/") + "/health")
        checks["stt_ready"] = {"ok": ok, "detail": detail}

        # NAS 挂载来源/标识
        nas = nas_mount_identity(self.nas_expected_source,
                                 self.nas_mount_point)
        checks["nas_mount"] = nas

        # IMP-04：只有 db/HA/MA 是关键（NAS/播放器/STT 不阻塞基本控制）
        critical = ["db_writable", "ha_reachable", "ma_reachable"]
        ready = all(checks[k].get("ok") for k in critical)
        return {"ready": ready, "checks": checks,
                "non_critical": ["stt_ready", "target_player_online"]}
