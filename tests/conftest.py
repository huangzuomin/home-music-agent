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
    from tests.fakes.fake_ma import FakeMA

    import context as context_mod

    fake_ha = FakeHA()
    fake_ha.states.update({
        "sensor.ma_target_player": "Squeezebox Touch",
        "sensor.ma_now_playing": "晴天，周杰伦",
    })
    fake_llm = FakeLLM()
    fake_ma = FakeMA()
    store = vg_app.session_mod.SessionStore(
        path=tmp_path / "agent-sessions.jsonl")
    fake_history = context_mod.TrackHistory(
        path=tmp_path / "track-history.jsonl")

    monkeypatch.setattr(vg_app, "ha", fake_ha)
    monkeypatch.setattr(vg_app, "_sessions", store)
    monkeypatch.setattr(vg_app, "get_sessions", lambda: store)
    monkeypatch.setattr(vg_app, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(vg_app, "_ma", fake_ma)
    monkeypatch.setattr(vg_app, "get_ma", lambda: fake_ma)
    monkeypatch.setattr(vg_app, "_history", fake_history)
    monkeypatch.setattr(vg_app, "get_history", lambda: fake_history)

    # IMP-03c：控制核心与存储单例按测试重建（否则跨测试持有旧 executor）
    from tests.fakes.fake_ha import FakeHA as _FakeHA  # noqa: F401
    ctrl_store = vg_app.storage_mod.ControlStore(
        db_path=tmp_path / "control.db")
    executor = vg_app.ha_executor_mod.HAExecutor(
        fake_ha, queue_resolver=lambda pid: fake_ma.queue_state(pid))
    coord = vg_app.coordinator_mod.CommandCoordinator(
        store=ctrl_store, executor=executor,
        default_player={"player_id": "sq-1", "player_name": "Squeezebox Touch"})
    monkeypatch.setattr(vg_app, "_control_store", ctrl_store)
    monkeypatch.setattr(vg_app, "get_control_store", lambda: ctrl_store)
    monkeypatch.setattr(vg_app, "_coordinator", coord)
    monkeypatch.setattr(vg_app, "get_coordinator", lambda: coord)

    client = TestClient(vg_app.app)
    return vg_app, client, fake_ha, fake_llm, store
