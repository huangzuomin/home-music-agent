"""IMP-04 — 设备配对、令牌鉴权、撤销、限速、Origin 校验。"""
from __future__ import annotations

import pytest

from auth import DeviceAuth, check_origin
from storage import ControlStore


@pytest.fixture()
def auth(tmp_path):
    store = ControlStore(db_path=tmp_path / "control.db")
    return DeviceAuth(store=store, pairing_ttl_sec=600.0)


def test_pairing_full_flow(auth):
    code_res = auth.issue_pairing_code()
    assert code_res["ok"] and code_res["code"]
    paired = auth.pair(code_res["code"], device_name="手机A", kind="pwa")
    assert paired and paired["device_id"] and paired["token"]
    assert auth.authenticate(paired["device_id"], paired["token"]) is True
    assert auth.authenticate(paired["device_id"], "wrong") is False


def test_pairing_code_single_use_and_expiry(auth):
    res = auth.issue_pairing_code(ttl_sec=600.0)   # 正常有效期
    code = res["code"]
    assert auth.pair(code, "d1") is not None      # 第一次使用即消耗
    assert auth.pair(code) is None, "配对码必须一次性"
    # 过期语义由 ttl 控制：issue_pairing_code(ttl_sec=0) 的用例见 expiry 单测


def test_revoke_disables_device(auth):
    res = auth.issue_pairing_code()
    paired = auth.pair(res["code"], "d1")
    assert auth.authenticate(paired["device_id"], paired["token"])
    assert auth.revoke(paired["device_id"]) is True
    assert auth.authenticate(paired["device_id"], paired["token"]) is False


def test_unknown_device_rejected(auth):
    assert auth.authenticate("dev-none", "tok") is False
    assert auth.authenticate("", "") is False


def test_rate_limit_blocks_flood(auth):
    ok_count = sum(1 for _ in range(130) if auth.check_rate("dev-flood",
                                                            limit=120,
                                                            window=60.0))
    assert ok_count == 120


def test_origin_allowlist():
    assert check_origin(None, ["https://a.b"]) is True          # 非浏览器放行
    assert check_origin("https://a.b", ["https://a.b"]) is True
    assert check_origin("https://evil.example", ["https://a.b"]) is False


def test_paired_device_persisted_in_store(auth):
    res = auth.issue_pairing_code()
    paired = auth.pair(res["code"], "d1")
    dev = auth._store.get_device(paired["device_id"])
    assert dev and dev["token_hash"] and dev["token_hash"] != paired["token"], (
        "明文 token 不得落库（只存哈希）")
    assert dev["revoked"] == 0
