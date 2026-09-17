"""T11 —— 同一 request_id 重试两次「下一首」，实际只切一次（IMP-03c 翻绿）。

对应 review：G03/G07。IMP-03c 起由 CommandCoordinator 提供幂等：
(device_id, request_id) 唯一约束——网络重试返回原命令，不重复执行
非幂等动作（next/previous）。
"""
from __future__ import annotations


def test_t11_retry_same_request_id_next__executes_once(vg):
    vg_app, client, fake_ha, _, _ = vg

    # 同一个 request_id 因网络重试发送两次
    for _ in range(2):
        client.post("/agent", json={
            "text": "下一首", "session_id": "s-t11",
            "request_id": "req-next-1"})

    # IMP-03c：写动作经 script.music_execute_v1 下发（ma_args 含 next）
    execs = [c for c in fake_ha.script_calls
             if c[0] == "music_execute_v1"
             and "next" in c[1].get("ma_command", "")]
    assert len(execs) == 1, (
        "同一 request_id 的重试应只执行一次 next，实际 %d 次" % len(execs))


def test_t22_companion_different_intent_not_rerouted(vg):
    """正向契约（T22 一部分）：控制只发给绑定的目标脚本，不产生其他播放器的动作。"""
    vg_app, client, fake_ha, _, _ = vg
    r = client.post("/agent", json={"text": "下一首", "session_id": "s-t22"})
    assert r.status_code == 200
    assert [c[0] for c in fake_ha.script_calls] == ["music_execute_v1"]
    assert "next" in fake_ha.script_calls[-1][1].get("ma_command", "")
