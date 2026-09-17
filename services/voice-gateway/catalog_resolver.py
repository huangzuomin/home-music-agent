#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-06 — 搜索决策（catalog_resolver）。

原则（计划 IMP-06 / R03/R13）：
    * 保留完整查询输入，区分 track / artist / album；
    * 可用性分类 full / preview / unavailable / unknown，附可验证依据；
    * 不确定的具体点歌最多给 3 个可选择候选；
    * 查询无结果时**不清当前队列**（调用方依据 matches 为空保持现状）。
"""
from __future__ import annotations

from typing import Any

MAX_CANDIDATES = 3


def classify_availability(uri: str, is_playable: bool | None = None,
                          source: str = "") -> str:
    """可用性分类（附依据的字段见返回结构）。

    library:// 且可播 → full；QQ 等在线流 → preview（60s 试听）；
    显式不可播 → unavailable；其余 unknown。
    """
    u = str(uri or "")
    if is_playable is False:
        return "unavailable"
    if u.startswith("library://"):
        return "full"
    low = u.lower()
    if "qqmusic" in low or u.lower().startswith("qq://")             or "preview" in source.lower():
        return "preview"
    return "unknown"


def detect_entity_type(query: str, candidates: list[dict[str, Any]]) -> str:
    """粗分实体类型：候选里 album 字段占优 → album；歌手名重合 → artist；
    默认 track。保留完整查询文本由调用方负责。"""
    q = str(query or "").strip()
    album_hits = artist_hits = track_hits = 0
    for c in candidates:
        if q and q in str(c.get("album") or ""):
            album_hits += 1
        if q and q in str(c.get("artist") or ""):
            artist_hits += 1
        if q and q in str(c.get("title") or ""):
            track_hits += 1
    if album_hits >= artist_hits and album_hits > 0:
        return "album"
    if artist_hits > track_hits:
        return "artist"
    return "track"


def resolve(query: str, candidates: list[dict[str, Any]],
            media_type: str = "track") -> dict[str, Any]:
    """从候选中选出最多 3 个可选项。

    返回 {"type", "matches": [{"title","artist","uri","availability"}...],
          "ambiguous": bool}
    matches 为空 = 无确定匹配（调用方保持当前队列，不清空）。
    """
    q = str(query or "").strip()
    scored: list[dict[str, Any]] = []
    for c in candidates:
        title = str(c.get("title") or c.get("name") or "")
        artist = str(c.get("artist") or "")
        uri = str(c.get("uri") or "")
        playable = c.get("is_playable")
        availability = classify_availability(uri, playable)
        exact = q and (q in title or q in artist)
        score = (2 if exact else 0) + (1 if availability == "full" else 0)
        scored.append({"title": title, "artist": artist, "uri": uri,
                       "availability": availability, "_score": score})
    scored.sort(key=lambda x: x["_score"], reverse=True)
    matches = [{k: m[k] for k in ("title", "artist", "uri", "availability")}
               for m in scored[:MAX_CANDIDATES]]
    ambiguous = len(scored) > MAX_CANDIDATES and all(
        m["availability"] == "full" for m in matches) and len(matches) == MAX_CANDIDATES
    return {"type": detect_entity_type(q, candidates), "matches": matches,
            "ambiguous": ambiguous}
