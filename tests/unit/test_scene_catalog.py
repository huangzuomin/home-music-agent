"""IMP-06 — 场景曲单与搜索决策（离线测试，样例配置自造）。"""
from __future__ import annotations

import pytest

import os
import sys



import yaml

from scene_catalog import (
    catalog_report, load_scenes, normalize_candidate, scene_readiness,
    satisfies_constraints,
)
from catalog_resolver import classify_availability, resolve


# ---------------------------------------------------------------- 固定数据

def _cand(title: str, minutes: int, vocal: str = "verified_no",
          offline: bool = True) -> dict:
    return {"uri": f"library://{title}", "title": title,
            "artist": "示例演奏者", "duration_sec": minutes * 60,
            "attributes": {"vocal": vocal}, "offline_verified": offline,
            "verified_at": "2026-09-18T09:00:00+08:00", "source": "library"}


def _scene(candidates: list[dict], enabled: bool = True,
           target: int = 45, no_vocal: bool = True) -> dict:
    return {"id": "focus", "name": "专注", "enabled": enabled,
            "coverage_target_min": target,
            "constraints": {"no_vocal": no_vocal},
            "candidates": candidates}


def test_load_scenes_from_yaml(tmp_path):
    p = tmp_path / "scenes.yaml"
    p.write_text(
        "scenes:\n"
        "  - id: focus\n"
        "    name: 专注\n"
        "    enabled: true\n"
        "    coverage_target_min: 45\n"
        "    constraints: {no_vocal: true}\n"
        "    candidates:\n"
        "      - {uri: 'library://1', title: Gymnopedie, artist: Satie,\n"
        "         duration_sec: 900, attributes: {vocal: verified_no},\n"
        "         offline_verified: true}\n", encoding="utf-8")
    scenes = load_scenes(p)
    assert len(scenes) == 1
    assert scenes[0]["candidates"][0]["vocal"] == "verified_no"


def test_load_scenes_missing_file_returns_empty(tmp_path):
    from scene_catalog import load_scenes as ls
    assert ls(tmp_path / "nope.yaml") == []


# ---------------------------------------------------------------- 就绪度

def test_focus_ready_when_coverage_meets_target():
    scene = _scene([_cand(f"古典 {i}", 16) for i in range(3)])  # 48min ≥ 45
    rep = scene_readiness(scene)
    assert rep["status"] == "ready" and rep["ready"] is True
    assert rep["coverage_min"] == 48.0


def test_focus_disabled_when_coverage_short():
    scene = _scene([_cand(f"古典 {i}", 10) for i in range(2)])  # 20min < 45
    rep = scene_readiness(scene)
    assert rep["status"] == "disabled" and rep["ready"] is False
    assert any("覆盖" in m for m in rep["missing"])


def test_disabled_scene_reported_disabled():
    scene = _scene([_cand("古典", 60)], enabled=False)
    rep = scene_readiness(scene)
    assert rep["status"] == "disabled" and rep["ready"] is False


def test_unknown_vocal_cannot_satisfy_no_vocal_constraint():
    """无人声硬约束只接受 verified_no；unknown 不能当满足（计划原文）。"""
    cand = _cand("疑似器乐", 20, vocal="unknown")
    assert satisfies_constraints(cand, {"no_vocal": True}) is False
    scene = _scene([cand])
    rep = scene_readiness(scene)
    assert rep["ready"] is False and rep["status"] == "disabled"
    assert any("未验证" in m for m in rep["missing"])


def test_catalog_report_lists_all_scenes():
    scenes = [_scene([_cand("古典", 50)], enabled=True),
              _scene([_cand("流行", 30)], enabled=False)]
    rep = catalog_report(scenes)
    assert [r["status"] for r in rep] == ["ready", "disabled"]


# ---------------------------------------------------------------- 解析器

def test_resolver_classifies_availability():
    r = resolve("晴天", [
        {"title": "晴天", "artist": "周杰伦", "uri": "library://31",
         "is_playable": True},
        {"title": "晴天 (Live)", "artist": "周杰伦",
         "uri": "qqmusic://999", "is_playable": True},
        {"title": "晴天 翻唱", "artist": "某人", "uri": "x://1",
         "is_playable": False},
    ])
    kinds = {m["uri"]: m["availability"] for m in r["matches"]}
    assert kinds["library://31"] == "full"
    assert kinds["qqmusic://999"] == "preview"
    assert kinds["x://1"] == "unavailable"


def test_resolver_detects_entity_type():
    cands = [{"title": "晴天", "artist": "周杰伦", "uri": "u1"}]
    assert resolve("周杰伦", cands)["type"] == "artist"
    assert resolve("晴天", cands)["type"] == "track"


def test_resolver_caps_candidates_at_three():
    cands = [{"title": f"晴天 {i}", "artist": "周", "uri": f"u{i}",
              "is_playable": True} for i in range(6)]
    r = resolve("晴天", cands)
    assert len(r["matches"]) <= 3


def test_resolver_no_result_returns_empty_matches():
    r = resolve("不存在的歌", [])
    assert r["matches"] == []
