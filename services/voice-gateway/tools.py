#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 5 — Music Agent 工具定义与执行。

工具清单（方案 §9 + v0.3 §14 扩展）：
    music_play            按自然语言描述搜索并播放
    music_transport       播放控制（暂停/继续/上下首/停止/音量）
    music_context         查询当前播放内容
    music_search          只读搜索曲库（v0.3 §14.2，返回候选不播放）
    music_record_feedback 记录显式反馈（v0.3 §14.4 / §15）

⚠️ 执行层的铁律（D13）：所有音乐动作**一律经 HA script 下发**，本模块不直连 MA。
   工具只负责「参数校验 + 映射到 HA script 名」，不自己发 HTTP。
   例外（2026-09-13 拍板）：music_search 是只读查询，由 /agent 流程经
   context.MAClient 执行，不产生任何写动作。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 工具 schema

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "music_play",
            "description": (
                "Search the music library and start playback. "
                "Use this for any request to play/listen music, including abstract "
                "moods or scenes (e.g. '适合写代码的安静音乐' -> query '安静的钢琴曲'). "
                "When the user refers to an artist, pass the artist name directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Search keywords. Either a concrete artist/song title, "
                            "or 2-6 Chinese keywords describing mood/genre/scene. "
                            "Never leave empty."
                        ),
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["track", "album", "artist", "playlist", "radio"],
                        "description": "What kind of entity to search for. Default track.",
                    },
                    "allow_backfill": {
                        "type": "boolean",
                        "description": (
                            "Whether to auto-download when the LOCAL library has no "
                            "match. true for concrete artist/song requests. "
                            "false for abstract mood/scene requests ('安静的钢琴曲') "
                            "-- never download for those, just play whatever matches."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_fetch",
            "description": (
                "Download a song into the local library in the background "
                "(async job). Use ONLY when the user names a concrete song/artist "
                "that is not in the library. It plays automatically when ready. "
                "Do NOT use for abstract mood/scene requests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Concrete song/artist keywords.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_transport",
            "description": (
                "Control the current playback. Use for pause/resume/next/previous/"
                "stop/volume changes. Do NOT use music_play for these. "
                "ABSOLUTE volume: when the user names a number or a percentage "
                "('音量调到30' / 'volume 40' / 'set volume to 20'), use "
                "action='volume_set' with level=that number. Relative words "
                "('大声一点' / '太吵了') use volume_up / volume_down. "
                "'静音'/'mute' = volume_set with level=0."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "pause", "resume", "next", "previous",
                            "stop", "volume_up", "volume_down", "volume_set",
                        ],
                        "description": "The playback action to perform.",
                    },
                    "level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "Required when action='volume_set': target volume "
                            "0-100 (0 = mute). Ignored for other actions."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_context",
            "description": (
                "Query what is currently playing (song title, artist, player state). "
                "Use when the user asks about the current track."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_search",
            "description": (
                "Search the music library WITHOUT playing. Returns candidate tracks. "
                "Use when the user asks to find/list options, or when you need to "
                "check what exists before deciding. Do NOT use for direct play requests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords (artist, title, or mood/scene words).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_record_feedback",
            "description": (
                "Record explicit user feedback about music. Use for statements like "
                "'这首不错' (strong_positive), '这个别再放' (strong_negative), "
                "'我不喜欢这个歌手' (artist_negative), '这种适合工作' (scene_positive). "
                "Pure recording; does not change playback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signal": {
                        "type": "string",
                        "enum": [
                            "strong_positive", "strong_negative",
                            "track_positive", "track_negative",
                            "artist_negative", "scene_positive",
                        ],
                        "description": "The feedback signal type.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Short note, e.g. scene name for scene_positive.",
                    },
                },
                "required": ["signal"],
            },
        },
    },
]

# ---------------------------------------------------------------- 执行映射

# 工具名 → HA script 名
TRANSPORT_SCRIPTS: dict[str, str] = {
    "pause": "music_pause",
    "resume": "music_resume",
    "next": "music_next",
    "previous": "music_previous",
    "stop": "music_stop",
    "volume_up": "music_volume_up",
    "volume_down": "music_volume_down",
    # 绝对音量（HA 侧新建的 script，内部走 MA players/cmd/volume_set）
    "volume_set": "music_volume_set",
}

VALID_ACTIONS = set(TRANSPORT_SCRIPTS)
VALID_MEDIA_TYPES = {"track", "album", "artist", "playlist", "radio"}


class ToolError(ValueError):
    """工具参数非法。"""


def plan(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """把一次 tool_call 翻译成执行计划。

    返回 {"kind": "script"|"query", "script": str|None, "variables": dict, "say": str}
    - kind=script  → 交给 HA script.turn_on
    - kind=query   → 只读查询，不发动作
    参数非法抛 ToolError。
    """
    if tool_name == "music_play":
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("music_play: query is required and must not be empty")
        media_type = str(arguments.get("media_type") or "track").strip()
        if media_type not in VALID_MEDIA_TYPES:
            media_type = "track"
        # 抽象场景查询（"安静的钢琴曲"）不该触发下载 —— 交给 music-fetcher 的
        # 过滤也拦不住，最稳的是让模型显式关掉。缺省 true：快路径点播都是具体歌曲。
        allow_bf = arguments.get("allow_backfill")
        allow_bf = True if allow_bf is None else bool(allow_bf)
        return {
            "kind": "script",
            "script": "music_play_query",
            "variables": {"query": query, "media_type": media_type,
                          "allow_backfill": allow_bf},
            "say": "好的，为你播放%s" % query,
        }

    if tool_name == "music_fetch":
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("music_fetch: query is required and must not be empty")
        # IMP-02：补库完成只入库（asset_ready），不再自动播放。
        return {
            "kind": "script",
            "script": "music_backfill",
            "variables": {"query": query},
            "say": "库里没有%s，我这就去找，下好后只加入曲库，不会自动播放"
                   % query,
        }

    if tool_name == "music_transport":
        action = str(arguments.get("action") or "").strip()
        if action not in VALID_ACTIONS:
            raise ToolError(
                "music_transport: invalid action %r (valid: %s)"
                % (action, ", ".join(sorted(VALID_ACTIONS)))
            )
        variables: dict[str, Any] = {}
        say = TRANSPORT_SAY.get(action, "")
        if action == "volume_set":
            # ★ 绝对音量：模型常把「音量调到30」塞进 volume_down，
            #   那样只会掉一格。必须有独立动作 + 数字参数（实测踩过）。
            raw = arguments.get("level")
            if raw is None:
                raise ToolError(
                    "music_transport: action='volume_set' requires 'level' (0-100)")
            try:
                level = int(round(float(raw)))
            except (TypeError, ValueError):
                raise ToolError("music_transport: level must be a number, got %r" % raw)
            level = max(0, min(100, level))
            variables = {"level": level}
            say = "已静音" if level == 0 else "音量已设为 %d" % level
        return {
            "kind": "script",
            "script": TRANSPORT_SCRIPTS[action],
            "variables": variables,
            "say": say,
        }

    if tool_name == "music_context":
        return {
            "kind": "query",
            "script": None,
            "variables": {},
            "say": "",
        }

    if tool_name == "music_search":
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ToolError("music_search: query is required and must not be empty")
        return {
            "kind": "ma_search",       # 由 /agent 流程用 context.MAClient 只读执行
            "script": None,
            "variables": {"query": query},
            "say": "",
        }

    if tool_name == "music_record_feedback":
        signal = str(arguments.get("signal") or "").strip()
        if signal not in VALID_SIGNALS:
            raise ToolError(
                "music_record_feedback: invalid signal %r (valid: %s)"
                % (signal, ", ".join(sorted(VALID_SIGNALS)))
            )
        return {
            "kind": "feedback",        # 由 /agent 流程写入 SessionStore
            "script": None,
            "variables": {"signal": signal,
                          "note": str(arguments.get("note") or "")[:200]},
            "say": "",
        }

    raise ToolError("unknown tool: %s" % tool_name)


VALID_SIGNALS = {
    "strong_positive", "strong_negative", "track_positive",
    "track_negative", "artist_negative", "scene_positive",
}


# IMP-02：HTTP 受理 ≠ 播放器确认，播报语改用受理措辞，不宣称已完成。
TRANSPORT_SAY: dict[str, str] = {
    "pause": "好的，正在暂停播放",
    "resume": "继续播放",
    "next": "下一首",
    "previous": "上一首",
    "stop": "好的，正在停止播放",
    "volume_up": "音量已调大",
    "volume_down": "音量已调小",
}
