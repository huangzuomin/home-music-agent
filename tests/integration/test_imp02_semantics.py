"""IMP-02 语义断言：闭麦拆出、静音恢复、虚假承诺消除（G03/G05，T09/T15/T17）。

全部离线：HA 用 FakeHA，MA 用 FakeMA（含实测 players 形状）。
"""
from __future__ import annotations

import json

from tests.fakes.fake_ma import FakeMA


def _vg_with_ma(vg, monkeypatch, tmp_path, sq_volume=88.0):
    """在 vg fixture 基础上注入 FakeMA / TrackHistory（Smart Path 需要）。"""
    vg_app, client, fake_ha, fake_llm, store = vg
    fake_ma = FakeMA()
    for p in fake_ma.players:
        if p["player_id"] == "sq-1":
            p["volume_level"] = sq_volume
    import context as context_mod

    fake_history = context_mod.TrackHistory(
        path=tmp_path / "track-history.jsonl")
    monkeypatch.setattr(vg_app, "_ma", fake_ma)
    monkeypatch.setattr(vg_app, "get_ma", lambda: fake_ma)
    monkeypatch.setattr(vg_app, "_history", fake_history)
    monkeypatch.setattr(vg_app, "get_history", lambda: fake_history)
    return vg_app, client, fake_ha, fake_llm, fake_ma


def test_mic_control_unsupported_via_command(vg):
    """说「闭麦」→ 显式 MIC_CONTROL_UNSUPPORTED，不执行任何音乐动作。"""
    vg_app, client, fake_ha, _, _ = vg
    r = client.post("/command", json={"text": "闭麦", "mode": "rules"})
    body = r.json()
    assert body["intent"] == "mic_control_unsupported"
    assert body.get("script") is None
    assert "设备" in body.get("say", "")
    assert fake_ha.script_calls == [], "闭麦不得触发任何 HA 脚本"


def test_unmute_restores_previous_volume_after_mute(vg, monkeypatch, tmp_path):
    """静音（记录静音前音量 88）→ 取消静音 → 恢复到 88 而非固定 40。"""
    vg_app, client, fake_ha, _, _ = _vg_with_ma(vg, monkeypatch, tmp_path)

    r1 = client.post("/command", json={"text": "静音"})
    assert r1.json()["intent"] == "music_mute"
    # IMP-03c：写动作经 music_execute_v1 短执行脚本（参数在 ma_args_json 里）
    def last_level():
        import json as _json
        name, var = fake_ha.script_calls[-1]
        assert name == "music_execute_v1", name
        return _json.loads(var["ma_args_json"])["volume_level"]

    r1 = client.post("/command", json={"text": "静音"})
    assert r1.json()["intent"] == "music_mute"
    assert last_level() == 0

    r2 = client.post("/command", json={"text": "取消静音"})
    body = r2.json()
    assert body["intent"] == "music_unmute"
    assert last_level() == 88, (
        "取消静音应恢复静音前的 88（FakeMA 提供 volume_level=88）")
    assert body.get("unmute_known") is True


def test_unmute_without_memory_uses_default_and_says_so(vg):
    """无静音前记录（如服务重启丢失）→ 保守默认 40，且如实说明是默认值。"""
    vg_app, client, fake_ha, _, _ = vg
    r = client.post("/command", json={"text": "取消静音"})
    body = r.json()
    assert body["intent"] == "music_unmute"
    assert body.get("unmute_known") is False, "无记录时不得冒充「已恢复原音量」"
    assert "默认" in body.get("say", ""), "应如实说明用的是默认音量"


def test_stop_uses_acceptance_phrasing_and_confirms_on_receipt(vg):
    """IMP-03c 契约：停止经控制核心执行，回执成功即 player_confirmed=True
    （确定性单写动作，收据级确认；快照级确认 IMP-08 接入）；
    播报使用受理措辞「正在停止播放」，不宣称已完成。"""
    vg_app, client, fake_ha, _, _ = vg
    r = client.post("/command", json={"text": "停止播放"})
    body = r.json()
    assert body["player_confirmed"] is True
    assert "正在停止" in body.get("say", ""), "应使用受理措辞，不宣称已完成"
    assert body.get("command_id"), "应回传 command_id 便于对账"


def test_constraint_reply_does_not_promise_future_adjustment(
        vg, monkeypatch, tmp_path):
    """仅更新约束（无播放动作）→ 不得回复「后面按这个来/后面已调整」。"""
    vg_app, client, fake_ha, fake_llm, _ = _vg_with_ma(vg, monkeypatch, tmp_path)
    fake_llm.canned = {
        "tool_calls": [{
            "function": {
                "name": "session_update",
                "arguments": json.dumps({"scene": "focus", "goal": "写代码"},
                                        ensure_ascii=False),
            }
        }],
        "content": "",
        "elapsed": 0.1,
        "usage": {},
    }
    r = client.post("/agent", json={
        "text": "我要专注工作了", "session_id": "s-honesty",
        "user_id": "default", "source": "voice", "want_say": True})
    body = r.json()
    assert body["intent"] != "agent_error", body
    reply = body.get("reply", "")
    assert "后面按这个来" not in reply and "后面已调整" not in reply, (
        "约束尚未生效，不得使用冒充生效的措辞：%r" % reply)
