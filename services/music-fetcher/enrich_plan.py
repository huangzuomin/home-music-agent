#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日充实的选题规划器（画像 v2）。

输入：无（读 taste 的三个 jsonl + MA 本地库 + 网易榜单）
输出：stdout 一行 JSON（与 recommend.py 同协议；日志走 stderr）

    {
      "ok": true, "mode": "profile_v2",
      "taste_seeds": [{"name", "weight", "reason"}],   # 口味池种子歌手
      "chart_seed":  {"kind": "song"|"artist", ...},   # 探索池种子
      "profile_summary": {...},                        # 给 HA 通知用
    }

选题配额：n 中 round(n*(1-EXPLORE_RATIO)) 个口味种子，其余给探索池。
冷启动：画像正池为空时，退化为「库内曲目最多的歌手」（v1 行为）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auto_backfill as AB  # noqa: E402
import charts as CH         # noqa: E402
import taste as TA          # noqa: E402

EXPLORE_RATIO = float(os.environ.get("ENRICH_EXPLORE_RATIO", "0.34"))


def log(*a):
    print(*a, file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="总选题数")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    n_taste = round(args.n * (1.0 - EXPLORE_RATIO))
    n_explore = max(args.n - n_taste, 1)

    log("=== ① 读本地库 ===")
    try:
        tracks = AB.library_tracks()
    except Exception as e:                              # noqa: BLE001
        print(json.dumps({"ok": False, "reason": "library_failed",
                          "error": str(e)}, ensure_ascii=False))
        return 1
    log("  库内 %d 首" % len(tracks))

    log("=== ② 构建口味画像 ===")
    profile = TA.build_profile(tracks=tracks)
    log("  信号：%s" % profile["signal_counts"])
    log("  正池：%s" % ", ".join(
        "%s(%.1f)" % (a["name"], a["weight"]) for a in profile["artists"][:6]))
    if profile["blacklist_artists"]:
        log("  黑名单歌手：%s" % "、".join(profile["blacklist_artists"]))

    seeds = TA.pick_taste_seeds(profile, k=n_taste)
    notes = []
    if not seeds:
        # 冷启动：退回 v1（库里最多歌手）
        top = AB.library_artists(tracks)
        if top:
            seeds = [{"name": top[0][0], "weight": 0.0}]
            notes.append("冷启动：播放数据不足，暂按库内歌手分布选题")
            log("  [!] 画像为空，冷启动种子：%s" % top[0][0])
        else:
            notes.append("冷启动：库与画像均为空，本期只走探索池")

    log("=== ③ 探索池（网易榜单）===")
    exclude_artists = set(profile["blacklist_artists"])
    exclude_songs = {k for k in profile["blacklist_songs"]}
    recent = TA.recent_backfill_queries()
    chart = None
    for _ in range(n_explore):                          # 目前每期 1 个
        chart = CH.pick_chart_seed(tracks, exclude_songs=exclude_songs,
                                   exclude_artists=exclude_artists)
        if chart:
            break
    if chart:
        log("  探索种子：%s" % chart)
    else:
        notes.append("榜单与降级池均不可用，本期全部走口味池")
        if not seeds:
            print(json.dumps({"ok": False, "reason": "no_seeds"},
                             ensure_ascii=False))
            return 1

    taste_seeds = [{"name": s["name"], "weight": s.get("weight", 0.0),
                    "reason": TA.explain_seed(s, profile)} for s in seeds]
    summary_parts = ["%s %s" % (s["name"], "(%.1f)" % s["weight"]
                                if s["weight"] else "(冷启动)")
                     for s in seeds]
    if chart:
        if chart["kind"] == "song":
            summary_parts.append("探索《%s》%s（%s）" % (
                chart["song_name"], chart["singers"], chart["source"]))
        else:
            summary_parts.append("探索 %s（%s）" % (
                chart["seed"], chart["source"]))
    summary = " + ".join(summary_parts)
    if notes:
        summary += "；" + "；".join(notes)

    out = {"ok": True, "mode": "profile_v2",
           "n": args.n, "explore_ratio": EXPLORE_RATIO,
           "taste_seeds": taste_seeds, "chart_seed": chart,
           "profile_summary": {
               "history_days": profile["history_days"],
               "signal_counts": profile["signal_counts"],
               "artists_top": profile["artists"][:8],
               "blacklist_artists": profile["blacklist_artists"],
           },
           "recent_queries_dedup": len(recent),
           "summary": summary, "notes": notes}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
