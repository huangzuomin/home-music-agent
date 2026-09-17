#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IMP-03a — 控制核心最小持久化（SQLite）。

设计约束（计划 §4.6 / ADR-01）：
    * MA 是播放器真实状态的唯一事实来源；本库不存第二份 now_playing。
    * 网关单写进程是本版本前提；多 worker 需另立协调设计。
    * 迁移可重复执行且结果一致（schema_migrations 记录版本）。

默认落盘路径：``data/control/control.db``（VM 持久卷；
本地/测试通过参数或 CONTROL_DB_PATH 覆盖——不在现场执行，部署等授权窗口）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migrations import MIGRATIONS, load_sql

DEFAULT_DB_PATH = Path(os.environ.get(
    "CONTROL_DB_PATH", str(Path("data") / "control" / "control.db")))

UTC = timezone.utc


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ControlStore:
    """控制核心的 SQLite 存储。线程安全（每线程独立连接）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migrate()

    # ------------------------------------------------------------ 连接
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ 迁移
    def migrate(self) -> list[int]:
        """按版本顺序应用迁移；可重复调用，结果一致。返回本次实际应用的版本。"""
        conn = self.connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {int(r["version"]) for r in
                   conn.execute("SELECT version FROM schema_migrations")}
        done: list[int] = []
        for version, filename in MIGRATIONS:
            if version in applied:
                continue
            conn.executescript(load_sql(version, filename))
            conn.execute("INSERT INTO schema_migrations (version, applied_at) "
                         "VALUES (?, ?)", (version, _utcnow_iso()))
            conn.commit()
            done.append(version)
        return done

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.connect().execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.connect().execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        self.connect().commit()

    # ------------------------------------------------------------ 命令（幂等）
    def record_command(self, device_id: str, request_id: str, action: str,
                       args: dict[str, Any] | None = None,
                       player_id: str = "", intent_epoch: int = 1,
                       command_id: str | None = None,
                       status: str = "accepted") -> dict[str, Any]:
        """登记命令。

        返回 {"command_id", "created", "conflict", "existing"}：
          created=True  新命令；
          created=False 且 conflict=False → 同 ID 同内容的重放，返回原命令；
          created=False 且 conflict=True  → 同 ID 不同内容（REQUEST_CONFLICT）。
        """
        import hashlib
        args_json = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
        fp = hashlib.sha256(
            json.dumps({"action": action, "args": args or {},
                        "player_id": player_id},
                       ensure_ascii=False, sort_keys=True)
            .encode("utf-8")).hexdigest()
        cid = command_id or uuid.uuid4().hex
        now = _utcnow_iso()
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO commands (command_id, device_id, request_id, action,"
                " args_json, player_id, intent_epoch, status, result_json,"
                " content_fp, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, device_id, request_id, action, args_json, player_id,
                 intent_epoch, status, None, fp, now, now))
            conn.commit()
            return {"command_id": cid, "created": True, "conflict": False}
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT command_id, content_fp, action, args_json FROM commands"
                " WHERE device_id = ? AND request_id = ?",
                (device_id, request_id)).fetchone()
            if row is None:
                raise
            conflict = row["content_fp"] != fp
            return {"command_id": row["command_id"], "created": False,
                    "conflict": conflict, "existing_action": row["action"]}
        finally:
            pass

    def get_command(self, device_id: str, request_id: str) -> dict[str, Any] | None:
        row = self.connect().execute(
            "SELECT * FROM commands WHERE device_id = ? AND request_id = ?",
            (device_id, request_id)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["args"] = json.loads(d.pop("args_json") or "{}")
        d["result"] = json.loads(d.pop("result_json") or "{}")
        return d

    def update_command_status(self, command_id: str, status: str,
                              result: dict[str, Any] | None = None) -> None:
        self.connect().execute(
            "UPDATE commands SET status = ?, result_json = COALESCE(?, result_json),"
            " updated_at = ? WHERE command_id = ?",
            (status, json.dumps(result, ensure_ascii=False) if result is not None
             else None, _utcnow_iso(), command_id))
        self.connect().commit()

    def supersede_pending(self, intent_epoch: int,
                          exclude_command_id: str | None = None) -> int:
        """STOP 屏障：使该 epoch 下所有未到终态的命令失效（不再派发/执行）。

        已派发的最小写动作不能被虚构取消——由执行对账（unknown）兜底。
        返回被标记 superseded 的命令数。
        """
        cur = self.connect().execute(
            "UPDATE commands SET status = 'superseded', updated_at = ?"
            " WHERE intent_epoch = ? AND status IN"
            " ('accepted', 'resolving', 'queued')"
            " AND (? IS NULL OR command_id != ?)",
            (_utcnow_iso(), intent_epoch, exclude_command_id,
             exclude_command_id))
        self.connect().commit()
        return cur.rowcount

    def reconcile_inflight(self) -> list[str]:
        """重启对账（计划 §4.2/X02）：executing/queued 且未达终态的命令标
        unknown（不盲重试非幂等动作）。返回受影响的 command_id 列表。
        """
        conn = self.connect()
        rows = conn.execute(
            "SELECT command_id FROM commands WHERE status IN"
            " ('accepted', 'resolving', 'queued', 'executing')").fetchall()
        ids = [r["command_id"] for r in rows]
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(f"UPDATE commands SET status = 'unknown',"
                         f" updated_at = ? WHERE command_id IN ({marks})",
                         (_utcnow_iso(), *ids))
            conn.commit()
        return ids

    def commands_in_epoch(self, intent_epoch: int) -> list[dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM commands WHERE intent_epoch = ?"
            " ORDER BY created_at", (intent_epoch,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json") or "{}")
            d["result"] = json.loads(d.pop("result_json") or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------ 设备与绑定
    def ensure_device(self, device_id: str, name: str = "",
                      kind: str = "api") -> None:
        self.connect().execute(
            "INSERT INTO devices (device_id, name, kind, created_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(device_id) DO NOTHING",
            (device_id, name, kind, _utcnow_iso()))
        self.connect().commit()

    def set_binding(self, scope: str, player_id: str,
                    player_name: str = "") -> None:
        self.connect().execute(
            "INSERT INTO player_bindings (scope, player_id, player_name,"
            " updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE"
            " SET player_id = excluded.player_id,"
            " player_name = excluded.player_name,"
            " updated_at = excluded.updated_at",
            (scope, player_id, player_name, _utcnow_iso()))
        self.connect().commit()

    def get_binding(self, scope: str = "default") -> dict[str, str] | None:
        row = self.connect().execute(
            "SELECT player_id, player_name FROM player_bindings"
            " WHERE scope = ?", (scope,)).fetchone()
        return {"player_id": row["player_id"],
                "player_name": row["player_name"] or ""} if row else None
