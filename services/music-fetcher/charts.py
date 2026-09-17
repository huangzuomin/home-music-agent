#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""榜单种子：从网易音乐公开榜单取「当天最受欢迎的歌」，作为每日充实的探索池。

为什么是网易而不是 QQ：
    网易 /api/playlist/detail 免登录、结构稳定（2026-09-13 从 VM1 实测可达）；
    QQ 音乐 musicu.fcg 接口参数多、字段易变，先不接（后续需要再加）。
    musicdl 音源里的 NeteaseMusicClient 与本榜单同生态，命中率自然更高。

榜单池（每天随机挑 1-2 个，增加多样性）：
    热歌榜 3778678 / 新歌榜 3779629 / 飙升榜 19723756

降级链：榜单接口失败 → 内置热门歌手池（静态，防茧房的保底探索）。
"""
from __future__ import annotations

import json
import os
import random
import re
import urllib.request
from typing import Any

CHART_API = "https://music.163.com/api/playlist/detail"
CHARTS: dict[str, int] = {
    "热歌榜": 3778678,
    "新歌榜": 3779629,
    "飙升榜": 19723756,
}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = float(os.environ.get("CHART_TIMEOUT", "12"))

# 榜单全挂时的保底探索池：华语热歌 + 经典（手动维护，宁少勿滥）
FALLBACK_ARTISTS = [
    "林俊杰", "邓紫棋", "薛之谦", "毛不易", "陈奕迅", "王菲",
    "五月天", "朴树", "李荣浩", "张惠妹", "陶喆", "孙燕姿",
]


def log(*a):
    import sys
    print(*a, file=sys.stderr)


def parse_chart_json(text: str) -> list[dict[str, Any]]:
    """解析 /api/playlist/detail 响应 → [{song_name, singers, rank}]。"""
    data = json.loads(text)
    tracks = ((data.get("result") or {}).get("tracks")) or []
    out: list[dict[str, Any]] = []
    for i, t in enumerate(tracks, 1):
        name = str(t.get("name") or "").strip()
        singers = "/".join(str((a or {}).get("name") or "").strip()
                           for a in (t.get("artists") or [])
                           if (a or {}).get("name")).strip("/")
        if name and singers:
            out.append({"song_name": name, "singers": singers, "rank": i})
    return out


def fetch_chart(list_id: int, timeout: float = TIMEOUT) -> list[dict[str, Any]]:
    url = "%s?id=%d" % (CHART_API, list_id)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_chart_json(r.read().decode("utf-8", "replace"))


def _norm(s: str) -> str:
    return re.sub(r"[\s　·・()（）\[\]【】\-_'\"‘’“”，，。.!?！？]", "",
                  str(s or "").lower())


def in_library(tracks: list[dict[str, Any]], name: str, singers: str) -> bool:
    """与 auto_backfill.in_library 同口径的独立实现（不引 heavy 依赖）。"""
    nn, ns = _norm(name), _norm(singers.split("/")[0])
    if not nn:
        return True
    for t in tracks:
        tname = _norm(t.get("name"))
        if not tname:
            continue
        tarts = [_norm((a or {}).get("name"))
                 for a in (t.get("artists") or [])]
        if nn in tname or tname in nn:
            if not ns or any(ns in ta or ta in ns for ta in tarts if ta):
                return True
    return False


def pick_chart_seed(tracks: list[dict[str, Any]],
                    exclude_songs: set[str] | None = None,
                    exclude_artists: set[str] | None = None,
                    rng: random.Random | None = None,
                    fetch=fetch_chart) -> dict[str, Any] | None:
    """从榜单随机挑一首「库里没有」的热门歌。

    返回 {"kind": "song", "song_name", "singers", "source"} 或
         {"kind": "artist", "seed", "source"}（降级池）；
    实在没得挑返回 None。
    """
    rng = rng or random
    exclude_songs = exclude_songs or set()
    exclude_artists = exclude_artists or set()

    chart_names = list(CHARTS)
    rng.shuffle(chart_names)
    for cname in chart_names:
        try:
            entries = fetch(CHARTS[cname])
        except Exception as e:                       # noqa: BLE001
            log("  [charts] %s 拉取失败：%s" % (cname, e))
            continue
        rng.shuffle(entries)                          # 榜内也随机，避免只推 Top1
        for c in entries:
            first = c["singers"].split("/")[0].strip()
            if first.lower() in {a.lower() for a in exclude_artists}:
                continue
            if _norm(c["song_name"] + "|" + first) in exclude_songs:
                continue
            if in_library(tracks, c["song_name"], c["singers"]):
                continue
            return {"kind": "song", "song_name": c["song_name"],
                    "singers": first, "source": "网易%s" % cname}
    # 降级：内置热门歌手池
    alive = [a for a in FALLBACK_ARTISTS
             if a.lower() not in {x.lower() for x in exclude_artists}]
    if alive:
        return {"kind": "artist", "seed": rng.choice(alive),
                "source": "内置热门池（榜单不可达降级）"}
    return None
