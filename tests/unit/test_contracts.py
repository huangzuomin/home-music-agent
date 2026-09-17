"""IMP-03a — 契约常量与数据结构。"""
from __future__ import annotations

from contracts import (
    ACTIONS,
    COMMAND_STATUSES,
    ERROR_HINTS,
    NON_IDEMPOTENT_ACTIONS,
    TERMINAL_STATUSES,
    CommandRequest,
    ERR_CAPABILITY_UNSUPPORTED,
    ERR_LLM_TIMEOUT,
    ERR_MIC_CONTROL_UNSUPPORTED,
    ERR_PLAYER_OFFLINE,
    ERR_REQUEST_CONFLICT,
    ERR_REVISION_CONFLICT,
    ERR_STATE_STALE,
    is_terminal,
)


def test_command_statuses_complete():
    assert COMMAND_STATUSES == {
        "accepted", "resolving", "queued", "executing",
        "player_confirmed", "failed", "superseded", "unknown",
    }
    assert TERMINAL_STATUSES <= COMMAND_STATUSES
    assert is_terminal("player_confirmed")
    assert not is_terminal("executing")


def test_actions_and_non_idempotent():
    assert {"next", "previous"} <= NON_IDEMPOTENT_ACTIONS
    assert "play_now" in ACTIONS and "stop" in ACTIONS
    assert NON_IDEMPOTENT_ACTIONS <= ACTIONS


def test_error_codes_required_by_plan():
    """计划 §4.5 列出的错误码必须齐全且每个都有中文提示。"""
    for code in (ERR_PLAYER_OFFLINE, ERR_STATE_STALE, ERR_LLM_TIMEOUT,
                 ERR_REVISION_CONFLICT, ERR_REQUEST_CONFLICT,
                 ERR_MIC_CONTROL_UNSUPPORTED, ERR_CAPABILITY_UNSUPPORTED):
        assert code in ERROR_HINTS, code
        assert ERROR_HINTS[code]


def test_request_fingerprint_idempotency_basis():
    """同内容同指纹（重放识别）；异内容异指纹（冲突识别，X05）。"""
    a = CommandRequest(device_id="d1", request_id="r1", action="next",
                       args={}, player_id="sq-1")
    a2 = CommandRequest(device_id="d1", request_id="r1", action="next",
                        args={}, player_id="sq-1")
    b = CommandRequest(device_id="d1", request_id="r1", action="volume_set",
                       args={"level": 30}, player_id="sq-1")
    assert a.content_fingerprint() == a2.content_fingerprint()
    assert a.content_fingerprint() != b.content_fingerprint()
