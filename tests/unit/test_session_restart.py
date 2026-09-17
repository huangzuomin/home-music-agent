"""T21 —— 命名 Session 重启后上下文恢复（当前为缺陷，xfail 复现）。

对应 review：G07。根因（源码级）：
SessionStore._new_named() 的 create 记录用**自动生成的 id**，随后的
update 记录才改名为调用方传入的 id；_replay() 回放时 update 因
``sid not in self._sessions`` 被跳过 → 命名会话重启后上下文全部丢失。
修复归属 IMP-02/03。
"""
from __future__ import annotations

import pytest

from session import SessionStore


def test_t21_named_session_context_survives_restart(tmp_path):
    """IMP-02 已修复：create 记录直接使用命名 id，回放完整恢复。"""
    store_file = tmp_path / "agent-sessions.jsonl"

    # ---- 第一次运行 ----
    s1 = SessionStore(path=store_file)
    sess, is_new = s1.get("desk-1", "u1")
    assert is_new
    s1.append_turn(sess, "播放周杰伦的晴天", "play_query", "好的，为你播放晴天")
    s1.update_scene(sess, scene="focus", goal="写代码")
    s1.add_feedback(sess, "track_positive",
                    target={"title": "晴天", "artist": "周杰伦",
                            "uri": "library://track/31"})

    # ---- 重启（新实例回放同一 jsonl）----
    s2 = SessionStore(path=store_file)
    sess2, is_new2 = s2.get("desk-1", "u1")

    assert not is_new2, "命名会话在重启后不应被当作新会话"
    assert any(t["user"].startswith("播放周杰伦")
               for t in sess2.get("conversation", [])), "对话上下文应恢复"
    assert sess2.get("scene") == "focus", "场景约束应恢复"
    assert sess2.get("feedback"), "显式反馈应恢复"
