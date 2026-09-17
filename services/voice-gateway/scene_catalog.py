#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-06 — 场景曲单与就绪度（scene catalog & readiness）。

原则（计划 IMP-06 / 风险表）：
    * 曲单由部署方以 YAML 显式配置（真实来源 ID + 属性 + 验证时间）；
      系统不编造曲目、不用自动下载凑数。
    * 覆盖目标默认 45 分钟；不足 → 场景显式 disabled，不冒充可用。
    * 无人声等硬约束只接受 verified 属性；unknown 不能当满足。
    * 离线可用以 offline_verified 标记为准（实际播放验证后才置 true）。

文件格式（config/scenes.example.yaml 有完整示例）：
    scenes:
      - id: focus
        name: 专注
        enabled: true
        coverage_target_min: 45
        constraints: {no_vocal: true}
        candidates:
          - uri: library://track/101
            title: Gymnopédie No.1
            artist: Erik Satie
            duration_sec: 210
            attributes: {vocal: verified_no}
            offline_verified: true
            verified_at: "2026-09-18T09:00:00+08:00"
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

COVERAGE_TARGET_MIN_DEFAULT = 45
VOCAL_VERIFIED_NO = "verified_no"
VOCAL_UNKNOWN = "unknown"

DEFAULT_SCENES_FILE = Path(os.environ.get(
    "SCENES_FILE",
    str(Path(__file__).resolve().parent / "config" / "scenes.yaml")))


def load_scenes(path: str | Path | None = None) -> list[dict[str, Any]]:
    """读取场景配置；文件缺失/为空时返回 []（/scenes 显式报告 disabled）。"""
    p = Path(path or DEFAULT_SCENES_FILE)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    scenes = data.get("scenes") or []
    return [normalize_scene(sc) for sc in scenes if isinstance(sc, dict)]


def normalize_scene(sc: dict[str, Any]) -> dict[str, Any]:
    constraints = sc.get("constraints") or {}
    return {
        "id": str(sc.get("id") or "").strip(),
        "name": str(sc.get("name") or "").strip(),
        "enabled": bool(sc.get("enabled", True)),
        "coverage_target_min": int(sc.get("coverage_target_min",
                                          COVERAGE_TARGET_MIN_DEFAULT)),
        "constraints": {"no_vocal": bool(constraints.get("no_vocal", False))},
        "candidates": [normalize_candidate(c)
                       for c in (sc.get("candidates") or [])
                       if isinstance(c, dict)],
    }


def normalize_candidate(c: dict[str, Any]) -> dict[str, Any]:
    attrs = c.get("attributes") or {}
    return {
        "uri": str(c.get("uri") or ""),
        "title": str(c.get("title") or ""),
        "artist": str(c.get("artist") or ""),
        "duration_sec": int(c.get("duration_sec") or 0),
        # 无人声属性：verified_no=已验证无人声；verified_yes=有人声；unknown=未验证
        "vocal": str(attrs.get("vocal") or VOCAL_UNKNOWN),
        "offline_verified": bool(c.get("offline_verified", False)),
        "verified_at": str(c.get("verified_at") or ""),
        "source": str(c.get("source") or "library"),
    }


def satisfies_constraints(cand: dict[str, Any],
                          constraints: dict[str, bool]) -> bool:
    """硬约束过滤：无人声只接受 verified_no；unknown 不能当满足（计划原文）。"""
    if constraints.get("no_vocal") and cand.get("vocal") != VOCAL_VERIFIED_NO:
        return False
    return True


def scene_readiness(scene: dict[str, Any]) -> dict[str, Any]:
    """场景就绪度：enabled + 覆盖达目标 → ready；否则显式 disabled 并给出原因。"""
    target = int(scene.get("coverage_target_min", COVERAGE_TARGET_MIN_DEFAULT))
    base = {"id": scene.get("id"), "name": scene.get("name"),
            "enabled": scene.get("enabled", True)}
    if not base["enabled"]:
        return {**base, "status": "disabled", "ready": False,
                "coverage_min": 0.0, "coverage_target_min": target,
                "candidates_ok": 0, "candidates_total": len(
                    scene.get("candidates") or []),
                "missing": ["场景被配置为停用"]}

    ok, unknown_vocal = [], 0
    cands = [normalize_candidate(c) for c in scene.get("candidates") or []]
    for c in cands:
        # 无人声约束排除的候选若属「人声未验证」，单独计数以便如实报告
        if not satisfies_constraints(c, scene.get("constraints") or {}):
            if c.get("vocal") == VOCAL_UNKNOWN:
                unknown_vocal += 1
            continue
        if c.get("offline_verified"):
            ok.append(c)
        if c.get("vocal") == VOCAL_UNKNOWN:
            unknown_vocal += 1

    total_min = sum(int(c.get("duration_sec") or 0) for c in ok) / 60.0
    ready = total_min >= target
    missing: list[str] = []
    if not ready:
        missing.append(f"覆盖 {total_min:.0f} 分钟 < 目标 {target} 分钟")
    if unknown_vocal:
        missing.append(f"{unknown_vocal} 首候选的人声属性未验证")
    if not scene.get("candidates"):
        missing.append("曲单为空")

    return {**base, "status": "ready" if ready else "disabled",
            "ready": ready, "coverage_min": round(total_min, 1),
            "coverage_target_min": target, "candidates_ok": len(ok),
            "candidates_total": len(scene.get("candidates") or []),
            "missing": missing}


def catalog_report(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [scene_readiness(s) for s in scenes]
