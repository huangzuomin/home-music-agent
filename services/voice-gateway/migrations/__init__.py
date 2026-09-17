"""IMP-03a — 版本化迁移。每个迁移只执行一次，重复 migrate 结果一致。

新增迁移：在 MIGRATIONS 列表追加 (next_version, "NNNN_name.sql")，
SQL 必须写成可重复执行（IF NOT EXISTS / IF EXISTS 风格）。
"""
from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent

# (version, filename) —— version 严格递增
MIGRATIONS: list[tuple[int, str]] = [
    (1, "0001_init.sql"),
]


def load_sql(version: int, filename: str) -> str:
    return (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
