"""pytest 全局配置：导入路径与离线环境。

测试铁律（IMP-01）：
    * 全部离线：不连家中 HA/MA/NAS，不需要任何 API Key，
      不触发音乐下载，不产生真实播放。
    * 被测模块以「模块级可替换依赖」方式打桩（monkeypatch 模块属性），
      不改变源码的默认行为语义。
"""
from __future__ import annotations

import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VG_DIR = REPO_ROOT / "services" / "voice-gateway"
MF_DIR = REPO_ROOT / "services" / "music-fetcher"
TEST_TMP = pathlib.Path(os.environ.get(
    "HMA_TEST_TMP", REPO_ROOT / ".pytest-tmp"))
TEST_TMP.mkdir(parents=True, exist_ok=True)

# ---- 环境变量必须在被测模块导入前就位（app.py 在 import 时读取）----
os.environ.setdefault("ENV_PATH", str(TEST_TMP / "missing.env"))   # 不存在 → 空配置
os.environ.setdefault("LOG_PATH", str(TEST_TMP / "gateway.log"))
os.environ.setdefault("HA_BASE", "http://fake-ha.invalid:8123")
os.environ.setdefault("AGENT_ENABLED", "true")
os.environ.setdefault("AGENT_SESSION_ENABLED", "true")
os.environ.setdefault("AGENT_HARD_TIMEOUT", "4")
os.environ.setdefault("AGENT_TIMEOUT", "5")
os.environ.setdefault("UNMUTE_LEVEL", "40")          # 与生产默认一致（T17 的缺陷前提）
os.environ.setdefault("TTS_ENABLED", "false")

for p in (str(MF_DIR), str(VG_DIR)):   # VG 后插入 → import app 命中网关
    if p not in sys.path:
        sys.path.insert(0, p)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402


@pytest.fixture()
def vg(monkeypatch, tmp_path):
    """导入 voice-gateway app 并隔离其全局单例。

    返回 SimpleNamespace：app 模块、TestClient、以及可替换的桩位
    （fake_ha / fake_llm / fake_ma / store）。
    """
    import app as vg_app
    from fastapi.testclient import TestClient

    from tests.fakes.fake_ha import FakeHA
    from tests.fakes.fake_llm import FakeLLM

    fake_ha = FakeHA()
    fake_ha.states.update({
        "sensor.ma_target_player": "Squeezebox Touch",
        "sensor.ma_now_playing": "晴天，周杰伦",
    })
    fake_llm = FakeLLM()
    store = vg_app.session_mod.SessionStore(
        path=tmp_path / "agent-sessions.jsonl")

    monkeypatch.setattr(vg_app, "ha", fake_ha)
    monkeypatch.setattr(vg_app, "_sessions", store)
    monkeypatch.setattr(vg_app, "get_sessions", lambda: store)
    monkeypatch.setattr(vg_app, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(vg_app, "_ma", None)
    monkeypatch.setattr(vg_app, "_history", None)
    monkeypatch.setattr(vg_app, "get_ma",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("此用例不应触碰真实/未打桩的 MA")))
    monkeypatch.setattr(vg_app, "get_history",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("此用例不应触碰未打桩的 TrackHistory")))

    client = TestClient(vg_app.app)
    return vg_app, client, fake_ha, fake_llm, store
