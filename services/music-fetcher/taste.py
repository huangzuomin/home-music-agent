#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口味画像：把播放行为 + 显式反馈 + 补库历史，聚合成「歌手权重表 + 黑名单」。

数据源（全部只读，都是别的服务已在写的 jsonl，本模块不产出任何数据）：
    track-history.jsonl   voice-gateway 写；op=track（曲目标题/歌手/uri）+ op=implicit
                          （上一曲的隐式信号：complete / skip_fast / skip_partial）
    agent-sessions.jsonl  voice-gateway 写；op=create / op=update(patch.feedback=[])
                          feedback 项 = {t, signal, target:{title,artist,uri}, note}
    jobs.jsonl            music-fetcher 写；历史补库（近 30 天查询词去重用）

信号权重（§方案确认版）：
    complete +3   skip_fast -2   skip_partial 0
    strong_positive +10   track_positive +5   scene_positive +2
    strong_negative -10（曲入黑名单）  track_negative -8（曲入黑名单）
    artist_negative → 歌手黑名单（权重表里也要剔除）

时间衰减：90 天半衰期，weight *= 0.5 ** (age_days / 90)。
冷启动：正权重歌手 < 1 个时由调用方回退旧行为（库里最多歌手）。

本模块纯标准库、不发任何网络请求 —— library 由调用方传入（可测）。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

# voice-gateway 的 sessions 目录（compose 里以只读方式挂进 music-fetcher）
SESSIONS_DIR = Path(os.environ.get("TASTE_SESSIONS_DIR", "/app/sessions"))
TRACK_HISTORY = Path(os.environ.get(
    "TASTE_TRACK_HISTORY", str(SESSIONS_DIR / "track-history.jsonl")))
AGENT_SESSIONS = Path(os.environ.get(
    "TASTE_AGENT_SESSIONS", str(SESSIONS_DIR / "agent-sessions.jsonl")))
FETCHER_JOBS = Path(os.environ.get(
    "TASTE_FETCHER_JOBS",
    str(Path(os.environ.get("FETCHER_DATA_DIR", "/app/data")) / "jobs.jsonl")))

HALF_LIFE_DAYS = float(os.environ.get("TASTE_HALF_LIFE_DAYS", "90"))
RECENT_QUERY_DAYS = 30

# 占位歌手名（context.py 同源）：标签缺失时不计分
_ARTIST_PLACEHOLDERS = {"", "[unknown]", "unknown", "未知", "未知艺术家",
                        "various artists"}

# 隐式信号 → 分值
IMPLICIT_WEIGHTS = {"complete": 3.0, "skip_fast": -2.0, "skip_partial": 0.0}
# 显式信号 → 分值（歌级）与特殊动作
EXPLICIT_WEIGHTS = {"strong_positive": 10.0, "track_positive": 5.0,
                    "scene_positive": 2.0,
                    "strong_negative": -10.0, "track_negative": -8.0}
BLACKLIST_SIGNALS = {"strong_negative", "track_negative"}   # 曲级黑名单


def log(*a):
    import sys
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- 基础工具
def parse_iso(s: str) -> float:
    """'2026-09-13T22:45:12+0800' → epoch 秒。解析失败返回 0（按最老处理）。"""
    s = str(s or "").strip()
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.timestamp() if dt.tzinfo else time.mktime(
                dt.timetuple())
        except ValueError:
            continue
    return 0.0


def iter_jsonl(path: Path):
    """逐行读 jsonl；坏行跳过。文件不存在时不报错。"""
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:                    # noqa: BLE001
                    continue
    except OSError:
        return


def norm(s: str) -> str:
    """归一化：小写 + 去空白与常见标点（与 auto_backfill.norm 思路一致但独立实现）。"""
    return re.sub(r"[\s　·・()（）\[\]【】\-_'\"‘’“”，，。.!?！？]", "",
                  str(s or "").lower())


def first_artist(singers: str) -> str:
    return re.split(r"[/&、,，;；]", str(singers or ""))[0].strip()


def clean_artist(name: str) -> str:
    n = str(name or "").strip()
    return "" if n.lower() in _ARTIST_PLACEHOLDERS else n


def _decay(t_epoch: float, now: float) -> float:
    if t_epoch <= 0:
        return 0.5                                      # 没有时间戳 → 半强度
    age_days = max(0.0, (now - t_epoch) / 86400.0)
    return 0.5 ** (age_days / max(HALF_LIFE_DAYS, 1.0))


def song_key(title: str, artist: str) -> str:
    return norm(title) + "|" + norm(first_artist(artist))


# ---------------------------------------------------------------- 画像构建
def build_profile(tracks: list[dict[str, Any]] | None = None,
                  now: float | None = None) -> dict[str, Any]:
    """构建画像。tracks = MA 本地库（用于 library:// 补齐歌手），可传 []。"""
    now = now or time.time()
    tracks = tracks if tracks is not None else []
    by_uri = {str(t.get("uri") or ""): t for t in tracks}

    scores: Counter = Counter()          # artist -> 加权分
    events = Counter()                   # 信号计数（可解释性）
    song_black: set[str] = set()         # 歌级黑名单 key
    artist_black: set[str] = set()
    first_seen = None
    oldest = now

    def add(artist: str, pts: float, t_epoch: float, signal: str) -> None:
        a = clean_artist(artist)
        if not a or a in artist_black:
            return
        w = _decay(t_epoch, now) * pts
        if w:
            scores[a] += w
        events[signal] += 1

    # ---- ① 播放行为（track-history.jsonl）
    current: dict[str, Any] = {}
    for rec in iter_jsonl(TRACK_HISTORY):
        op = rec.get("op")
        if op == "track":
            uri = str(rec.get("uri") or "")
            artist = clean_artist(rec.get("artist"))
            if (not artist or artist.lower() == "[unknown]") \
                    and uri in by_uri:
                arts = [clean_artist((a or {}).get("name"))
                        for a in (by_uri[uri].get("artists") or [])]
                artist = next((x for x in arts if x), "")
            current = {"uri": uri, "title": str(rec.get("title") or ""),
                       "artist": artist,
                       "t": parse_iso(rec.get("t"))}
            if current["t"]:
                oldest = min(oldest, current["t"])
                first_seen = first_seen or current["t"]
        elif op == "implicit" and current:
            sig = str(rec.get("implicit") or "")
            pts = IMPLICIT_WEIGHTS.get(sig)
            if pts:
                add(current["artist"], pts, current["t"], sig)
            if sig == "skip_fast":
                # 曲级弱黑名单：快跳的歌 30 天内不再推（只记 key，不分 artist）
                pass

    # ---- ② 显式反馈（agent-sessions.jsonl）
    for rec in iter_jsonl(AGENT_SESSIONS):
        fbs = []
        if rec.get("op") == "create":
            fbs = ((rec.get("session") or {}).get("feedback")) or []
        elif rec.get("op") == "update":
            fbs = ((rec.get("patch") or {}).get("feedback")) or []
        for fb in fbs:
            sig = str(fb.get("signal") or "")
            target = fb.get("target") or {}
            artist = clean_artist(target.get("artist"))
            if (not artist) and target.get("uri") in by_uri:
                arts = [clean_artist((a or {}).get("name"))
                        for a in ((by_uri.get(target["uri"]) or {})
                                  .get("artists") or [])]
                artist = next((x for x in arts if x), "")
            t_epoch = parse_iso(fb.get("t"))
            if sig == "artist_negative":
                if artist:
                    artist_black.add(artist)
                    scores.pop(artist, None)
                events[sig] += 1
                continue
            pts = EXPLICIT_WEIGHTS.get(sig)
            if pts is None:
                continue
            if sig in BLACKLIST_SIGNALS:
                key = song_key(target.get("title"), artist)
                if key != "|":
                    song_black.add(key)
            add(artist, pts, t_epoch, sig)

    artists = [{"name": n, "weight": round(w, 2)}
               for n, w in scores.most_common() if w > 0]
    span_days = round((now - oldest) / 86400.0, 1) if oldest < now else 0.0
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "history_days": span_days,
        "signal_counts": dict(events),
        "artists": artists,
        "blacklist_artists": sorted(artist_black),
        "blacklist_songs": sorted(song_black),
    }


def recent_backfill_queries(path: Path | None = None,
                            days: int = RECENT_QUERY_DAYS) -> set[str]:
    """近 N 天补库用过的查询词（norm 后），用于选题去重。"""
    cutoff = time.time() - days * 86400
    out: set[str] = set()
    for j in iter_jsonl(path or FETCHER_JOBS):
        try:
            if float(j.get("created") or 0) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        q = norm(j.get("query"))
        if q:
            out.add(q)
    return out


# ---------------------------------------------------------------- 选题
def pick_taste_seeds(profile: dict[str, Any], k: int = 2,
                     rng=None) -> list[dict[str, Any]]:
    """按权重加权随机抽 k 个种子歌手（不放回）。黑名单歌手不参与。

    权重只决定概率不决定结果 —— 这是防茧房的核心：周杰伦再常听，
    每天也有相当概率抽到别人。
    """
    rng = rng or __import__("random")
    pool = [a for a in profile.get("artists", [])
            if a["name"] not in set(profile.get("blacklist_artists", []))
            and a["weight"] > 0]
    if not pool:
        return []
    k = min(k, len(pool))
    names: list[str] = []
    picked: list[dict[str, Any]] = []
    while len(picked) < k:
        weights = [a["weight"] for a in pool
                   if a["name"] not in names]
        alive = [a for a in pool if a["name"] not in names]
        if not alive:
            break
        chosen = rng.choices(alive, weights=weights, k=1)[0]
        names.append(chosen["name"])
        picked.append(dict(chosen))
    return picked


def explain_seed(a: dict[str, Any], profile: dict[str, Any]) -> str:
    """给通知用的理由串：'周杰伦（权重 12.3，最近常听）'。"""
    return "%s（画像权重 %.1f）" % (a["name"], a["weight"])
