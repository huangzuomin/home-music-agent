"""IMP-04 — liveness/readiness 分组件核验（桩注入，全离线）。"""
from __future__ import annotations

import pytest

import os
import sys

sys.path.insert(0, "D:/Work/home-music-agent/services/voice-gateway")

import health as health_mod
from health import HealthChecker


def make_checker(nas_ok=True, ha_ok=True, ma_ok=True, player_ok=True,
                 db_ok=True, stt_ok=True) -> HealthChecker:
    c = HealthChecker(
        db_path="unused.db",
        ha_base="http://fake-ha.invalid:8123", ha_token="",
        ma_probe=lambda: ma_ok,
        target_player_available=lambda: player_ok,
        stt_base="http://fake-stt.invalid:8100",
        nas_expected_source="", nas_mount_point="/mnt/music",
        control_db_probe=lambda: db_ok,
        ha_probe=lambda: (ha_ok, "stub"),
        stt_probe=lambda: (stt_ok, "stub"),
    )
    c._nas_stub = nas_ok
    return c


@pytest.fixture()
def patch_nas(monkeypatch):
    """把 NAS 挂载核验打桩为可控行为（Windows 无 /proc/mounts）。"""
    def _patch(result: dict):
        def fake(expected_source, mount_point):
            return dict(result, ok=result.get("ok", False))
        monkeypatch.setattr(health_mod, "nas_mount_identity", fake)
    return _patch


def test_liveness_always_ok():
    c = make_checker()
    assert c.liveness()["ok"] is True


def test_readiness_all_green(patch_nas):
    c = make_checker(nas_ok=True)
    patch_nas({"ok": True, "source": "nas:/music"})
    rep = c.readiness()
    assert rep["ready"] is True
    assert all(rep["checks"][k]["ok"] for k in
               ("db_writable", "ha_reachable", "ma_reachable",
                "nas_mount"))


def test_readiness_nas_mismatch_blocks_ready(patch_nas):
    """X08：NAS 未挂载/来源不匹配 → readiness False（不得写宿主空目录）。"""
    c = make_checker()
    patch_nas({"ok": False, "detail": "not mounted"})
    rep = c.readiness()
    assert rep["ready"] is False
    assert rep["checks"]["nas_mount"]["ok"] is False


def test_readiness_stt_degraded_does_not_block_ready(patch_nas):
    """STT 属非关键组件：故障不拉低 ready（基本播放控制不受影响）。"""
    c = make_checker()
    patch_nas({"ok": True})
    rep = c.readiness()
    assert rep["ready"] is True
    assert "stt_ready" in rep["non_critical"]


def test_readiness_db_failure_blocks_ready():
    c = make_checker(db_ok=False)
    rep = c.readiness()
    assert rep["ready"] is False
    assert rep["checks"]["db_writable"]["ok"] is False
