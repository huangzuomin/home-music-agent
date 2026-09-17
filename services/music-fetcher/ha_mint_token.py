#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Home Assistant 铸造一个**长效访问令牌（LLAT）**并写回 .env。

为什么需要它：
  HA 的普通 access_token 只有 **1800 秒** 寿命。music-fetcher 里的
  ``auto_backfill.py`` 靠 ``HA_ACCESS_TOKEN`` 调 ``script.music_play_uri``，
  一旦过期就 401 → 完整版播放回调静默失败（实测 2026-09-13 19:47 那次
  《晴天》补库 played=False 就是这么来的）。

  代码里虽然有「401 → 用 refresh_token 续期 → 重试一次」的兜底，但一旦 HA
  把来源 IP 记进失败计数，连续期请求本身也会被拒 —— 兜底不可靠。
  正解是换成**永不过期的 LLAT**。

做法（全部走本机，不需要浏览器）：
  1. 用 .env 里的 HA_REFRESH_TOKEN 换一个临时 access_token；
  2. 用它对 HA 的 websocket API 认证，发 ``auth/long_lived_access_token``
     命令，拿到 lifespan=3650 天的令牌；
  3. 把新令牌写回 .env 的 ``HA_ACCESS_TOKEN``（其余行原样保留）。

用法：
  python3 ha_mint_token.py                 # 铸造并写回 .env（会先备份）
  python3 ha_mint_token.py --dry-run       # 只铸造并打印，不改 .env
  python3 ha_mint_token.py --client-name music-fetcher --days 3650
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_PATH = Path(os.environ.get("ENV_PATH", "/opt/home-music-agent/.env"))


def log(*a):
    print(*a, file=sys.stderr if False else sys.stdout, flush=True)


def read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read() or b"{}")


def refresh_access_token(base: str, refresh_token: str, client_id: str) -> dict:
    return _post_form(base + "/auth/token", {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })


# ⚠️ 2026-09-13 实测：refresh_token 的 `client_id` 必须与**当初签发它时的那个**
#    完全一致，否则 HA 直接回 400 {"error":"invalid_request"}。
#    .env 里 HA_URL 是 http://192.168.1.50:8123，而令牌是在
#    http://127.0.0.1:8123/ 这个 client 下签发的 —— 这就是
#    「补库成功但完整版没播」的根因（auto_backfill.ha_call 用的是
#    HA_BASE + "/"，永远续期失败）。
#    这里按可能性从高到低挨个试，命中哪个就把哪个记下来给 ha_call 用。
CANDIDATE_CLIENT_IDS = (
    "http://127.0.0.1:8123/",
    "http://192.168.1.50:8123/",
    "http://localhost:8123/",
)


def refresh_with_fallback(base: str, refresh_token: str,
                          extra: str | None = None) -> tuple[dict, str]:
    """依次尝试候选 client_id，返回 (token响应, 成功的 client_id)。"""
    cands = ([extra] if extra else []) + [c for c in CANDIDATE_CLIENT_IDS
                                          if c != extra]
    last = None
    for cid in cands:
        try:
            tok = refresh_access_token(base, refresh_token, cid)
        except urllib.error.HTTPError as e:
            last = "client_id=%s -> HTTP %s %s" % (cid, e.code, e.read()[:120])
            continue
        if tok.get("access_token"):
            return tok, cid
        last = "client_id=%s -> %s" % (cid, tok)
    raise RuntimeError("所有候选 client_id 都失败；最后一次：%s" % last)


def mint_llat(ws_url: str, access_token: str, client_name: str,
              days: int) -> str:
    """用 websocket 的 auth/long_lived_access_token 命令铸造长效令牌。"""
    import websocket  # websocket-client

    ws = websocket.create_connection(ws_url, timeout=15)
    try:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError("HA websocket 未要求认证，响应=%s" % hello)

        ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError("websocket 认证失败：%s" % auth)

        ws.send(json.dumps({
            "id": 1,
            "type": "auth/long_lived_access_token",
            "client_name": client_name,
            "lifespan": days,
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            if not msg.get("success"):
                raise RuntimeError("铸造失败：%s" % msg.get("error"))
            return msg["result"]
    finally:
        try:
            ws.close()
        except Exception:
            pass


def write_env(path: Path, updates: dict) -> Path:
    """就地更新 .env 指定键，其余行（含注释）原样保留。返回备份路径。"""
    backup = path.with_suffix(path.suffix + ".bak-%s" % time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(path, backup)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                out.append("%s=%s" % (k, updates[k]))
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return backup


def main() -> int:
    ap = argparse.ArgumentParser(description="铸造 HA 长效访问令牌并写回 .env")
    ap.add_argument("--env", default=str(ENV_PATH), help=".env 路径")
    ap.add_argument("--client-name", default="music-fetcher",
                    help="令牌名字（HA 里可见）")
    ap.add_argument("--days", type=int, default=3650, help="有效期（天）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不改 .env")
    args = ap.parse_args()

    env = read_env(Path(args.env))
    base = (env.get("HA_URL") or "http://127.0.0.1:8123").rstrip("/")
    refresh_token = env.get("HA_REFRESH_TOKEN") or ""
    if not refresh_token:
        log("[FAIL] .env 里没有 HA_REFRESH_TOKEN，无法自动铸造。"
            "请在 HA 网页端「个人资料 → 安全 → 长期访问令牌」手动创建一个后写入 .env")
        return 2

    client_id = env.get("HA_CLIENT_ID") or ""
    log("[1/3] 用 refresh_token 换临时 access_token …")
    try:
        tok, used_cid = refresh_with_fallback(base, refresh_token,
                                              client_id or None)
    except Exception as exc:
        log("[FAIL] 续期失败：%s" % exc)
        log("       全部候选 client_id 都不可用 → 说明 refresh_token 已失效。")
        log("       手动兜底：HA 网页端 → 左下角用户名 → 安全 → "
            "长期访问令牌 → 创建，把令牌写进 .env 的 HA_ACCESS_TOKEN")
        return 3
    access_token = tok.get("access_token") or ""
    new_refresh = tok.get("refresh_token") or ""
    if not access_token:
        log("[FAIL] 响应里没有 access_token：%s" % tok)
        return 3
    log("       OK（access_token %d 字符，%s 秒后过期，client_id=%s）"
        % (len(access_token), tok.get("expires_in"), used_cid))

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    log("[2/3] websocket 铸造长效令牌（%d 天）…" % args.days)
    try:
        llat = mint_llat(ws_url, access_token, args.client_name, args.days)
    except Exception as exc:
        log("[FAIL] 铸造失败：%s: %s" % (type(exc).__name__, exc))
        log("       手动兜底：HA 网页端 → 左下角用户名 → 安全 → 长期访问令牌 → 创建")
        return 4
    log("       OK：%s…（%d 字符）" % (llat[:24], len(llat)))

    # 自检：拿新令牌真切一次 /api/
    req = urllib.request.Request(base + "/api/",
                                 headers={"Authorization": "Bearer " + llat})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log("       自检 /api/ -> %s" % resp.status)
    except Exception as exc:
        log("[FAIL] 新令牌自检失败：%s" % exc)
        return 5

    if args.dry_run:
        log("[dry-run] 未修改 %s" % args.env)
        log("LLAT=%s" % llat)
        return 0

    log("[3/3] 写回 %s …" % args.env)
    updates = {"HA_ACCESS_TOKEN": llat, "HA_CLIENT_ID": used_cid}
    if new_refresh:
        updates["HA_REFRESH_TOKEN"] = new_refresh
    backup = write_env(Path(args.env), updates)
    log("       OK（备份：%s）" % backup)
    log("       同时记下 HA_CLIENT_ID=%s，供 auto_backfill 续期用" % used_cid)
    log("")
    log("接着重启 music-fetcher 让新令牌生效：")
    log("  cd /opt/home-music-agent && docker compose up -d --force-recreate music-fetcher")
    return 0


if __name__ == "__main__":
    sys.exit(main())
