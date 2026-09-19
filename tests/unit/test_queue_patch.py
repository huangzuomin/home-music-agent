"""IMP-10 — 未来队列修改与撤销（离线测试）。"""
from __future__ import annotations

import pytest

from queue_patch import build_patch, apply_patch, build_undo, apply_undo


def test_patch_replace_tail():
    queue = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    patch = build_patch(0, queue, [{"title": "X"}])
    result = apply_patch(queue, patch)
    assert result == [{"title": "A"}, {"title": "X"}]


def test_patch_keeps_current():
    queue = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    patch = build_patch(1, queue, [{"title": "X"}, {"title": "Y"}])
    result = apply_patch(queue, patch)
    assert [t["title"] for t in result[:2]] == ["A", "B"]


def test_undo_restores():
    queue_before = [{"title": "A"}, {"title": "B"}]
    undo = build_undo(queue_before, 1)
    restored = apply_undo(queue_before, undo)
    assert restored == queue_before


def test_undo_roundtrip():
    queue_orig = [{"title": "A"}, {"title": "B"}]
    undo = build_undo(queue_orig, 1)
    restored = apply_undo(queue_orig, undo)
    assert restored == queue_orig
