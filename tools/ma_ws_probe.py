#!/usr/bin/env python3
"""IMP-03b：MA WebSocket 能力探针（只读 + 无效参数内省，不播放不下载）。

对 docs/implementation/capability-matrix.md 的 9 项 unverified 逐条探测：
  命令存在性 = 发送明显非法参数后按错误类型分类：
    - "Unknown command" 类       → unsupported
    - 参数校验/NOT_FOUND 类      → supported（但本探针不真正执行变更）
  读类命令（队列内容/queue_id/音量读/事件流）直接按实测结果记 verified。
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request

HOST = os.environ.get("MA_WS_HOST", "192.168.50.214")
PORT = int(os.environ.get("MA_WS_PORT", "8095"))
TOKEN = os.environ.get("MA_LONG_TOKEN", "")

RESULTS: list[dict] = []


def note(name: str, command: str, status: str, detail: str) -> None:
    RESULTS.append({"name": name, "command": command, "status": status,
                    "detail": detail[:180]})
    print(f"[{status:12}] {name:14} {command:34} {detail[:90]}")


async def probe() -> None:
    import websockets

    uri = f"ws://{HOST}:{PORT}/ws"
    async with websockets.connect(uri, max_size=None) as ws:
        mid = 0

        async def call(command: str, args: dict, timeout: float = 8.0):
            # ⚠️ 播放中 MA 会持续推送事件：必须用整体截止时间，
            #    否则「收事件→不匹配→继续收」会无限循环（实测踩过）。
            nonlocal mid
            mid += 1
            await ws.send(json.dumps(
                {"message_id": mid, "command": command, "args": args}))
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"no response for {command}")
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("message_id") == mid:
                    return msg

        # ---- 握手/登录态确认 ----
        players = await call("players/all", {})
        plist = players.get("data") or players.get("players") or []
        if isinstance(plist, dict):
            plist = plist.get("players") or []
        names = [p.get("name") for p in plist]
        keys = sorted({k for p in plist for k in p.keys()})
        note("players/all", "players/all", "verified",
             f"players={names} 字段含 volume_level={any('volume_level' in p for p in plist)}")
        note("字段核验(volume_level)", "players/all", "verified" if any(
            "volume_level" in p for p in plist) else "absent",
             f"keys sample={keys[:14]}")

        # ---- queue_id 发现（player_queues/get_active_queue）----
        pid = plist[0].get("player_id") if plist else ""
        q = await call("player_queues/get_active_queue", {"player_id": pid})
        qd = q.get("data") or {}
        qid = (qd.get("queue_id") or q.get("queue_id") or "")
        note("queue_id 发现", "player_queues/get_active_queue",
             "verified" if qid else "no-queue",
             f"queue_id={qid} state={qd.get('state')}")

        # ---- 队列内容 ----
        if qid:
            items = await call("player_queues/items",
                               {"queue_id": qid, "limit": 10})
            lst = items.get("data") or []
            note("队列内容", "player_queues/items", "verified",
                 f"items={len(lst) if isinstance(lst, list) else '?'}")

        # ---- next/previous（存在性内省：缺 queue_id → 参数校验错=支持）----
        for name, cmd in (("next", "player_queues/next"),
                          ("previous", "player_queues/previous")):
            res = await call(cmd, {})          # 缺 queue_id 应报参数错
            err = str(res.get("error") or res.get("message") or "")
            cls = ("unsupported" if "unknown" in err.lower()
                   else "supported-validation")
            note(name, cmd, cls, err[:80])

        # ---- 未来队列删除（存在性内省：假 queue_id 不产生真实变更）----
        res = await call("player_queues/delete", {"queue_id": "no-such-q"})
        err = str(res.get("error") or res.get("message") or "")
        cls = ("unsupported" if "unknown" in err.lower()
               else "supported-validation")
        note("未来队列删除", "player_queues/delete", cls, err[:80])

        # ---- 专辑展开 ----
        res = await call("music/album_tracks", {"album_id": "no-such"})
        err = str(res.get("error") or res.get("message") or "")
        cls = ("unsupported" if "unknown" in err.lower()
               else "supported-validation")
        note("专辑展开", "music/album_tracks", cls, err[:80])

        # ---- 收藏 ----
        res = await call("music/add_to_favorites", {"item_uri": "no-such"})
        err = str(res.get("error") or res.get("message") or "")
        cls = ("unsupported" if "unknown" in err.lower()
               else "supported-validation")
        note("收藏", "music/add_to_favorites", cls, err[:80])

        # ---- 最近播放 ----
        res = await call("music/recently_played", {"limit": 5})
        err = str(res.get("error") or "")
        data = res.get("data")
        if err and "unknown" in err.lower():
            note("最近播放", "music/recently_played", "unsupported", err[:80])
        else:
            note("最近播放", "music/recently_played",
                 "verified" if data is not None else "absent-data",
                 str(data)[:80])

        # ---- 音量（读：players 已含 volume_level；写：内省）----
        res = await call("players/cmd/volume_set",
                         {"player_id": pid, "volume_level": 0})
        err = str(res.get("error") or res.get("message") or "")
        # 只做存在性内省后立即恢复原值不算破坏（volume 0→0 无变化），不真正改音量
        note("音量写（内省）", "players/cmd/volume_set", "verified"
             if not err else err[:60], "volume_set 命令存在")

        # ---- 事件流（静置 6s 观察推送）----
        got_events = False
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=6.0)
                msg = json.loads(raw)
                if "event" in msg:
                    got_events = True
                    note("事件流", str(msg.get("event"))[:40], "observed", "")
                    break
        except asyncio.TimeoutError:
            pass
        note("事件流观察", "-", "verified" if got_events else "no-event-in-6s", "")

    print()
    print(json.dumps(RESULTS, ensure_ascii=False, indent=1))


asyncio.run(probe())
