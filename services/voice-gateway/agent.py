#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 升级（v0.3）— Planner（需求文档 §10/§11/§12/§28）。

与旧 run_agent（app.py 慢路径）的区别：
    旧：用户一句话 → LLM → 单个工具。无上下文、无会话、无指代解析。
    新：Music Context + Session + 最近对话一起进 prompt → LLM 选工具，
        由 /agent 流程执行并把结果回写 Session。

指代解析策略（Sprint 1 拍板）：**不写规则**。把 now_playing、队列摘要、
最近播放、最近对话原样塞进 system prompt，让模型自己消解
"这个 / 刚才 / 还是 / 再 / 后面 / 这种"。规则层只负责一件事：
检测到这类词时强制走 Agent 路径（见 app.py CONTEXT_REF）。

播报语策略（文档 §9）：动作本身就是回答。本模块产出的 reply 一律
短句（≤25 字）或空串，由 /agent 流程决定播不播报。
"""
from __future__ import annotations

import json
import time
from typing import Any

import llm as llm_mod
import tools as tools_mod


AGENT_SYSTEM_PROMPT = """你是家庭音乐 Agent 的决策核心。用户用中文口语表达音乐需求。

你会收到一段 JSON 格式的 Music Context，包含：当前播放器与播放内容(now_playing)、
后续队列(queue_summary)、最近播放(recent_tracks)、当前会话(session，含场景与约束)、
最近几轮对话(conversation)。

决策规则：
1. 「这个/这首/刚才/这种」指 now_playing 里正在播的歌；「后面」指后续队列；
   「还是/再」表示对当前会话约束的追加修改。结合 conversation 理解，不要当成新请求。
2. 用户表达场景/目标/约束（如"要写两小时东西，安静点，别有人唱"）：
   先调 session_update 记录 scene/constraints，再调 music_play 用 2-6 个具体中文
   关键词开始播放（例如"安静的钢琴曲 轻音乐"）。
3. 用户调整感觉时，先调 session_update 更新约束，再按抱怨的对象选动作：
   - 抱怨音量（"有点吵"/"太响了"）→ 调 music_transport(volume_down)；
     （"太小声"/"听不见"）→ music_transport(volume_up)。
   - **用户报出具体数字**（"音量调到30"/"声音开到一半"/"音量40"/"静音"）→
     调 music_transport(action="volume_set", level=数字)。数字就是目标音量(0-100)，
     静音=level 0；"一半"≈50，"最大"≈100。**不要**用 volume_down 代替绝对音量。
   - 抱怨音乐本身（"太闹了"/"想舒缓点"/"后面快一点"/"慢一点"）→
     调 music_play，用调整后的关键词换一批。
   - 用户明说不要打断当前这首 → 只更新约束，不做动作。
4. 反馈类表达（"这首不错"/"别再放这首"/"我不喜欢这个歌手"）：
   调 music_record_feedback，不要打断当前播放。
5. 询问当前播放（"这首是什么"/"刚才那首叫什么"）：调 music_context。
   "刚才那首"指 recent_tracks 里当前曲目之前的那一首。
6. 暂停/继续/上下首/停止/音量：调 music_transport。
7. 具体歌手/歌名：调 music_play，query 直接填原名，allow_backfill=true
   （本地库没有就后台下载，下好自动换成完整版）。
7b. 抽象场景/心情/风格（"安静的钢琴曲""适合写代码的""来点爵士"）：
   调 music_play，query 填 2-6 个中文关键词，**allow_backfill=false** ——
   这类描述没有确定的"原曲"，绝对不要触发下载。
7c. 用户明确要"把某首歌下载/加到库里"：调 music_fetch（只下载，不立刻播放）。
8. 与音乐完全无关的（闲聊、问天气）：不调任何工具，也不要输出任何文字。
9. 一次可以调多个工具：session_update 可以和音乐动作同时调。
10. 用户说「闭麦/关麦克风/取消闭麦」是**输入设备控制**，不是音乐指令：
    不要调用任何音乐工具。

只输出工具调用，不要输出解释文字、不要输出 JSON 或占位符。"""


SESSION_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_update",
        "description": (
            "Update the current music session's scene/goal/constraints. Call this "
            "whenever the user states or modifies a listening scenario (work, relax, "
            "party), a duration, or preferences (no vocals, low energy, not sad, "
            "faster tempo, quieter). Can be called together with a music action tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scene": {"type": "string",
                          "description": "Scenario tag, e.g. work/relax/sleep/party."},
                "goal": {"type": "string",
                         "description": "User's stated goal in their own words, short."},
                "duration_min": {"type": "integer",
                                 "description": "Planned listening duration in minutes, 0 if unspecified."},
                "constraints": {
                    "type": "object",
                    "description": "Constraint key-values, e.g. {\"vocals\":\"avoid\",\"energy\":\"low\",\"mood\":\"calm_not_sad\",\"tempo\":\"faster\"}.",
                },
            },
        },
    },
}


def _build_messages(text: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    ctx_json = json.dumps(context, ensure_ascii=False)
    return [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content":
            "Music Context:\n%s\n\n用户说：%s" % (ctx_json, text)},
    ]


def _args_of(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("function", {}).get("arguments") or "{}"
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _clean_reply_draft(content: str) -> str:
    """清洗「无工具调用」时 LLM 的 content。

    ⚠️ 2026-09-13 实测：GLM-4-Flash 在决定不调工具时会返回 "{}" 这类空壳文本，
    直接当回复播报就是「{}」——必须当空处理。只保留真正像人话的内容。
    """
    s = (content or "").strip()
    if not s:
        return ""
    if s in ("{}", "[]", "null", "None", '""', "''"):
        return ""
    # 纯符号/括号/空白组成的串一律丢弃
    if all(ch in "{}[]()<>\"'` \t\n\r:," for ch in s):
        return ""
    return s[:100]


def run(text: str, context: dict[str, Any],
        llm: llm_mod.LLMClient) -> dict[str, Any]:
    """执行一次 Planner 推理。

    返回 {
      "intent": str,
      "action": {"tool": name, "args": dict, "plan": tools.plan() 结果} | None,
      "session_updates": [ {scene/goal/duration_min/constraints}, ... ],
      "llm_seconds": float, "llm_usage": dict,
      "error": str (仅失败时),
    }
    """
    t0 = time.time()
    tools = tools_mod.TOOL_SCHEMAS + [SESSION_UPDATE_SCHEMA]
    try:
        res = llm.chat_with_tools(
            messages=_build_messages(text, context),
            # max_tokens 只够吐 tool_call 即可。实测 DeepSeek 关闭思考后
            # 输出 9~140 token，400 留足余量（200 曾导致 session_update 的
            # arguments 被截断成半截 JSON）。
            tools=tools, temperature=0.2, max_tokens=400,
        )
    except llm_mod.LLMError as e:
        return {"intent": "agent_error", "action": None, "session_updates": [],
                "error": str(e)[:200],
                "llm_seconds": round(time.time() - t0, 3), "llm_usage": {}}

    base = {"llm_seconds": res["elapsed"], "llm_usage": res.get("usage") or {}}
    calls = res["tool_calls"] or []

    session_updates: list[dict[str, Any]] = []
    action: dict[str, Any] | None = None

    for call in calls:
        fn = call.get("function", {}).get("name", "")
        args = _args_of(call)
        if fn == "session_update":
            session_updates.append({
                "scene": str(args.get("scene") or "")[:50],
                "goal": str(args.get("goal") or "")[:100],
                "duration_min": int(args.get("duration_min") or 0),
                "constraints": args.get("constraints")
                    if isinstance(args.get("constraints"), dict) else {},
            })
            continue
        if action is not None:
            continue  # 只执行第一个动作类工具，防连播两次
        try:
            planned = tools_mod.plan(fn, args)
        except tools_mod.ToolError as e:
            return {"intent": "agent_toolerror", "action": None,
                    "session_updates": session_updates,
                    "error": str(e)[:200], **base}
        action = {"tool": fn, "args": args, "plan": planned}

    if action is None and not session_updates:
        return {"intent": "agent_noop", "action": None,
                "session_updates": [],
                "reply_draft": _clean_reply_draft(res.get("content") or ""),
                **base}

    intent = "agent_" + (action["tool"] if action else "session_update")
    return {"intent": intent, "action": action,
            "session_updates": session_updates, **base}
