"""IMP-10 — 未来队列修改与撤销（保留当前曲，只换后面）。"""
from __future__ import annotations


def build_patch(current_index: int, current_queue: list[dict],
                new_tail: list[dict]) -> dict:
    """生成未来队列差分 patch。

    契约：
      * current_index 之前（含当前曲）不动
      * 只修改 current_index 之后的条目
    """
    return {
        "op": "replace_tail",
        "current_index": current_index,
        "keep_count": current_index + 1,
        "new_tail": new_tail,
    }


def apply_patch(queue: list[dict], patch: dict) -> list[dict]:
    """将 patch 应用到当前队列，返回新队列。"""
    keep = queue[:patch["current_index"] + 1]
    return keep + patch["new_tail"]


def build_undo(queue_before: list[dict], queue_revision: int = 1) -> dict:
    """构建撤销操作：还原到 queue_before 状态。"""
    return {
        "op": "undo",
        "queue_revision": queue_revision,
        "restore": queue_before,
    }


def apply_undo(queue: list[dict], undo: dict) -> list[dict]:
    """撤销队列修改，还原到 undo["restore"] 状态。"""
    return list(undo.get("restore") or [])
