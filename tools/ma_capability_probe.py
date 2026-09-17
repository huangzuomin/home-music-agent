#!/usr/bin/env python3
"""IMP-03b：MA 能力探针 v2（HTTP POST /api 命令内省 + WS 被动事件监听）。

只读原则：
  * 读类命令直接实测；
  * 变更类命令用「非法参数」内省：Unknown command = 不支持；
    参数/目标校验错误 = 支持（本探针不真正执行变更）。
"""
from __future__ import annotations

import json
import os
import urllib.request

MA = os.environ.get("MA_URL", "http://127.0.0.1:8095").rstrip("/")
TOKEN = os.environ["MA_LONG_TOKEN"]

RESULTS: list[dict] = []


def call(command: str, args: dict | None = None, timeout: float = 15.0):
    body = json.dumps({"command": command, "args": args or {}}).encode()
    req = urllib.request.Request(MA + "/api", body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def probe(name: str, command: str, args: dict, read: bool = False,
          pick=None) -> None:
    try:
        res = call(command, args)
    except Exception as e:
        RESULTS.append({"name": name, "command": command,
                        "status": "error", "detail": str(e)[:120]})
        print(f"[error      ] {name:14} {command:30} {str(e)[:70]}")
        return
    err = ""
    if isinstance(res, dict):
        err = str(res.get("error") or res.get("message") or "")
    detail = json.dumps(res, ensure_ascii=False)[:140] if not err else err
    status = ("verified" if not err else
              "unsupported" if "unknown" in err.lower() else
              "validation-error")
    if read and status == "verified" and pick:
        detail = str(pick(res))[:140]
    RESULTS.append({"name": name, "command": command, "status": status,
                    "detail": detail})
    print(f"[{status:16}] {name:14} {command:28} {detail[:100]}")


print("=== MA 能力探针 v2 ===")

# ① players（读，核验 volume_level / active / queue_id 字段）
probe("players 列表", "players/all", {}, read=True,
      pick=lambda r: {"count": len(r),
                      "keys": sorted({k for p in r for k in p.keys()})[:16],
                      "volume_level_present": any(
                          "volume_level" in p for p in r),
                      "active_present": any("active" in p for p in r),
                      "queue_id_present": any("queue_id" in p for p in r)})

# 找默认播放器 id（绑定用）
pid = ""
try:
    players = call("players/all")
    for p in players if isinstance(players, list) else []:
        if "squeezebox" in str(p.get("type", "")).lower():
            pid = p.get("player_id") or ""
            break
except Exception:
    pass

# ② queue_id 发现（显式 player_id）
probe("queue_id 发现", "player_queues/get_active_queue",
      {"player_id": pid} if pid else {}, read=True,
      pick=lambda r: {"queue_id": r.get("queue_id"),
                      "state": r.get("state"),
                      "current": (r.get("current_item") or {}).get("name")})

# ③ 队列内容
qid = ""
try:
    q = call("player_queues/get_active_queue", {"player_id": pid})
    qid = (q or {}).get("queue_id") or ""
except Exception:
    pass
if qid:
    probe("队列内容", "player_queues/items",
          {"queue_id": qid, "limit": 10}, read=True,
          pick=lambda r: {"items": len(r),
                          "first": (r[0].get("name") if r else None)})
else:
    print("[skip    ] 队列内容（无 queue_id）")

# ④⑤ next/previous 存在性内省（缺 queue_id → 参数校验错 = 支持）
probe("next 存在性", "player_queues/next", {}, read=True)
probe("previous 存在性", "player_queues/previous", {}, read=True)

# ⑥ 未来队列删除（假 queue_id 内省）
probe("队列删除存在性", "player_queues/delete", {"queue_id": "no-such-q"},
      read=True)

# ⑦ 专辑展开（假 id 内省）
probe("专辑展开存在性", "music/album_tracks", {"album_id": "no-such"},
      read=True)

# ⑧ 收藏（假 uri 内省）
probe("收藏存在性", "music/add_to_favorites", {"item_uri": "library://none"},
      read=True)

# ⑨ 最近播放
probe("最近播放", "music/recently_played_items", {"limit": 5}, read=True,
      pick=lambda r: json.dumps(r, ensure_ascii=False)[:120])

# ⑩ 音量写命令存在性（不真正执行：缺 player_id → 参数校验错 = 支持）
probe("音量写存在性", "players/cmd/volume_set", {}, read=True)

# ⑪ LLM 之外：search（已有生产证据，复核）
probe("music/search 复核", "music/search",
      {"search_query": "晴天", "media_types": ["track"], "limit": 3},
      read=True, pick=lambda r: len(r.get("tracks") or []))

print()
print(json.dumps(RESULTS, ensure_ascii=False, indent=1))
