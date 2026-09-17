#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推荐器：基于本地口味 / 指定种子，推荐「库里还没有」的曲目。

为什么需要它
------------
MA 自带的 `music/recommendations` 实测 **19 行 0 条**（2026-09-13）：
16 行基于本地库历史（库小 + 没播放记录 → 全空），3 行来自 QQ（未登录 → 0 条）。
等于这套系统**没有推荐能力**。本脚本补上这一层：

    口味种子（本地库出现最多的歌手 / 显式指定 / 场景词如「安静的钢琴曲」）
      → musicdl 在线搜索（聚合音源，一次几十条候选）
      → 剔除库里已有的（in_library）+ 过守卫（翻唱/片段/串烧/假时长）
      → 按作品去重 → top-N

产出可直接喂给 music-fetcher 补库（`--submit N`）。

用法
----
  python3 recommend.py --auto --limit 10            # 按本地口味推荐 10 首
  python3 recommend.py --seed 周杰伦 --limit 10
  python3 recommend.py --seed 安静的钢琴曲 --limit 8
  python3 recommend.py --seed 周杰伦 --submit 3     # 推荐并直接补库 3 首
  python3 recommend.py --auto --json                # 机器可读

产出协议（与 auto_backfill 一致）：日志走 stderr，`--json` 时 stdout 一行 JSON。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import auto_backfill as AB  # noqa: E402  （纯标准库，可安全复用）

FETCHER = os.environ.get("FETCHER_URL", "http://127.0.0.1:8300").rstrip("/")
DEFAULT_SOURCES = [s for s in os.environ.get(
    "SOURCES", "JBSouMusicClient,MituMusicClient,NeteaseMusicClient").split(",") if s]


def log(*a):
    print(*a, file=sys.stderr)


# ---------------- 口味画像 ----------------
def library_artists(tracks):
    """从本地库提取歌手 -> 曲目数（降序）。"""
    c = Counter()
    for t in tracks:
        for a in (t.get("artists") or []):
            n = str((a or {}).get("name") or "").strip()
            if n and n != "[unknown]":
                c[n] += 1
    return c.most_common()


def pick_seed(tracks):
    """自动选种子：库里曲目最多的歌手。没有就返回空串。"""
    top = library_artists(tracks)
    return top[0][0] if top else ""


_ARTIST_SEP = "[/&、,，;；]"


def song_key(name, singer=""):
    """作品去重键（与 auto_backfill.strip_version 同源）。

    ⚠️ 歌手必须按**所有常见分隔符**取第一位：只认 "/" 的话，
    「周杰伦, 温岚」和「周杰伦, 温岚, 吴宗宪」会被当成两个作品，
    于是《屋顶》被推荐了两次（实测踩到）。
    """
    import re
    first = re.split(_ARTIST_SEP, str(singer or ""))[0]
    return (AB.norm(AB.strip_version(name)), AB.norm(first))


# ---------------- 推荐 ----------------
def recommend(seed, tracks, sources, limit=10, min_duration=60, size=15,
              max_size_mb=0):
    """返回「库里还没有」的推荐曲目列表。

    排序沿用 musicdl_fetch 的 rank_key（search --json 输出已排好序），
    这里只做过滤与去重，**不重新排序** —— 避免两处排序逻辑不一致。

    max_size_mb>0 时剔除超大文件：聚合音源里 flac 常见 100~200MB/首，
    不设上限的话补十几首就能吃掉几 GB（群晖再大也经不起这么造）。
    """
    rc, out, err = AB.online_candidates(seed, sources, size=size)
    cands = AB.parse_candidates_json(out)
    if not cands:
        log("  [!] 在线搜索无结果。stderr 尾部：")
        log("  " + "\n  ".join([l for l in (err or "").splitlines() if l.strip()][-5:]))
        return []

    log("  种子「%s」→ 候选 %d 条，逐个过滤：\n" % (seed, len(cands)))
    picked, seen = [], set()
    n_in_lib = n_bad = n_dup = n_big = 0
    for c in cands:
        why = AB.reject_reason(c, seed, min_duration)
        if not why:
            if AB.in_library(tracks, c["song_name"], c["singers"]):
                why = "库里已有"
                n_in_lib += 1
        if not why and max_size_mb and c.get("size_mb", 0) > max_size_mb:
            why = "体积 %.1fMB > %dMB" % (c.get("size_mb", 0), max_size_mb)
            n_big += 1
        if why:
            if why != "库里已有":
                n_bad += 1
            continue
        k = song_key(c["song_name"], c["singers"])
        if k in seen:
            n_dup += 1
            continue
        seen.add(k)
        picked.append(c)
        log("  [推荐] %-30s %-18s %6s %7.1fMB %s" %
            (c["song_name"][:30], c["singers"][:18], c["dur"], c["size_mb"], c["ext"]))
        if len(picked) >= limit:
            break

    log("\n  统计：库里已有 %d / 守卫或体积拒 %d / 重复 %d / 推荐 %d"
        % (n_in_lib, n_bad, n_dup, len(picked)))
    return picked


# ---------------- 提交补库 ----------------
def submit(items, n, play=False):
    """把推荐结果提交给 music-fetcher 补库。"""
    done = []
    for c in items[:n]:
        q = "%s %s" % (c.get("singers") or "", c.get("song_name") or "")
        q = q.strip()
        payload = json.dumps({"query": q, "play": bool(play), "pick": 1}).encode()
        req = urllib.request.Request(
            FETCHER + "/backfill", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read() or b"null")
            log("  [提交] %-34s -> job %s (%s)" %
                (q[:34], res.get("job_id"), res.get("status")))
            done.append({"query": q, "job_id": res.get("job_id"),
                         "status": res.get("status")})
        except urllib.error.HTTPError as e:
            log("  [!] 提交失败 %s：HTTP %s" % (q, e.code))
            done.append({"query": q, "error": "http_%s" % e.code})
        except Exception as e:                       # noqa: BLE001
            log("  [!] 提交失败 %s：%s" % (q, e))
            done.append({"query": q, "error": str(e)})
    return done


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="推荐「库里还没有」的曲目")
    ap.add_argument("--seed", help="种子：歌手名 / 场景词（如「安静的钢琴曲」）")
    ap.add_argument("--auto", action="store_true", help="自动取库里曲目最多的歌手作种子")
    ap.add_argument("--limit", type=int, default=10, help="推荐条数")
    ap.add_argument("--size", type=int, default=15, help="每音源搜索结果数")
    ap.add_argument("--min-duration", type=int, default=60, help="最短时长（秒）")
    ap.add_argument("--max-size-mb", type=int, default=0,
                    help="剔除超过此体积的候选（聚合音源 flac 常见 100~200MB/首）；"
                         "0 = 不限")
    ap.add_argument("--sources", nargs="+", default=DEFAULT_SOURCES)
    ap.add_argument("--submit", type=int, metavar="N",
                    help="把前 N 条推荐直接提交给 music-fetcher 补库")
    ap.add_argument("--play", action="store_true", help="补库成功后播放（默认否）")
    ap.add_argument("--json", action="store_true", help="stdout 输出机器可读 JSON")
    args = ap.parse_args()

    if not AB.MA_TOKEN:
        log("[!] 没有 MA_TOKEN / MA_LONG_TOKEN。先加载：")
        log("    set -a && . /opt/home-music-agent/.env && set +a")
        if args.json:
            print(json.dumps({"ok": False, "reason": "no_ma_token"}))
        return 1

    log("=== ① 读本地库 ===")
    try:
        tracks = AB.library_tracks()
    except Exception as e:                           # noqa: BLE001
        log("  [!] 查库失败：%s" % e)
        return 1
    log("  库内 %d 首" % len(tracks))

    profile = library_artists(tracks)
    if profile:
        log("  口味画像（歌手 / 首数）：%s" %
            "、".join("%s %d" % (n, c) for n, c in profile[:6]))

    if args.seed:
        seed = args.seed.strip()
        how = "显式指定"
    elif args.auto or profile:
        seed = pick_seed(tracks)
        how = "本地口味自动推导"
    else:
        log("  [!] 库是空的，也没给 --seed —— 无法推导口味。")
        return 1
    if not seed:
        log("  [!] 推导不出种子歌手（库里没有带歌手元数据的曲目）。请显式 --seed。")
        return 1
    log("  种子：%s（%s）" % (seed, how))

    log("")
    log("=== ② 在线搜索 + 过滤（%s）===" % ",".join(args.sources))
    try:
        items = recommend(seed, tracks, args.sources, limit=args.limit,
                          min_duration=args.min_duration, size=args.size,
                          max_size_mb=args.max_size_mb)
    except Exception as e:                           # noqa: BLE001
        log("  [!] 推荐失败：%s: %s" % (type(e).__name__, e))
        if args.json:
            print(json.dumps({"ok": False, "reason": "recommend_failed",
                              "error": str(e)}, ensure_ascii=False))
        return 1

    result = {"ok": True, "seed": seed, "seed_from": how,
              "library_size": len(tracks), "profile": profile[:8],
              "recommendations": items, "submitted": []}

    if not items:
        log("\n[结果] 没有可推荐的（候选全是库里已有的或被守卫拒掉）")
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 0

    log("\n[结果] 推荐 %d 首：\n" % len(items))
    for i, c in enumerate(items, 1):
        log("  %2d. %-30s %-18s %6s  %s" %
            (i, c["song_name"][:30], c["singers"][:18], c["dur"], c["ext"]))

    if args.submit:
        log("\n=== ③ 提交补库（前 %d 条）===" % args.submit)
        result["submitted"] = submit(items, args.submit, play=args.play)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
