"""T11 —— 同一 request_id 重试两次「下一首」，实际只切一次（当前为缺陷，xfail）。

对应 review：G03/G07（请求受理、执行确认与幂等未分离）。
根因（源码级）：/command 与 /agent 均不接收 request_id，也没有任何
幂等约束——网络重试会原样重复执行非幂等动作（next/previous）。
修复归属 IMP-03（CommandCoordinator：认证设备 + request_id 唯一约束）。
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=True, reason="G03/T11：request_id 无幂等约束，修复在 IMP-03")
def test_t11_retry_same_request_id_next__executes_once(vg):
    vg_app, client, fake_ha, _, _ = vg

    # 同一个 request_id 因网络重试发送两次（当前 API 甚至不接收该字段）
    for _ in range(2):
        client.post("/agent", json={
            "text": "下一首", "session_id": "s-t11",
            "request_id": "req-next-1"})

    nexts = [name for name, _ in fake_ha.script_calls if name == "music_next"]
    assert len(nexts) == 1, (
        "同一 request_id 的重试应只执行一次 next，实际执行了 %d 次" % len(nexts))


def test_t22_companion_different_intent_not_rerouted(vg):
    """正向契约（T22 一部分）：控制只发给绑定的目标脚本，不产生其他播放器的动作。"""
    vg_app, client, fake_ha, _, _ = vg
    r = client.post("/agent", json={"text": "下一首", "session_id": "s-t22"})
    assert r.status_code == 200
    assert fake_ha.scripts_called() == ["music_next"]
