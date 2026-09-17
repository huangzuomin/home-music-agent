#!/usr/bin/env python3
"""IMP-04 — 只读 PRECHECK 延伸（preflight）。

在部署主机上运行（python3 tools/preflight.py --json）：
    * 容器/镜像（id 与版本）
    * 端口监听
    * NAS 挂载来源与容量
    * .env 键名齐全性（不读值）
    * 控制库可写、STT 模型文件在位
输出 markdown（人类可读）与 --json（机读）。只读，不改任何状态。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(os.environ.get("HMA_APP_DIR", "/opt/home-music-agent"))
EXPECTED_ENV_KEYS = [
    "HA_URL", "HA_USERNAME", "HA_PASSWORD", "MA_URL", "MA_USERNAME",
    "MA_PASSWORD", "STT_URL", "MA_LONG_TOKEN", "ZHIPU_API_KEY", "ZHIPU_BASE",
    "ZHIPU_MODEL", "LLM_API_KEY", "LLM_BASE", "LLM_MODEL",
    "HA_ACCESS_TOKEN", "HA_REFRESH_TOKEN", "HA_CLIENT_ID",
]
REQUIRED_TABLES = ["devices", "player_bindings", "commands",
                   "conversation_sessions", "listening_sessions",
                   "schema_migrations", "feedback_events", "meta"]


def sh(cmd: list[str], timeout: float = 30.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception as e:
        return f"__error__ {e}"


def containers() -> list[dict]:
    out = sh(["docker", "ps", "--format",
              "{{.Names}}\t{{.Image}}\t{{.Status}}"])
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"name": parts[0], "image": parts[1],
                         "status": parts[2]})
    return rows


def image_info(name: str) -> dict:
    iid = sh(["docker", "inspect", name, "--format", "{{.Image}}"]).strip()
    created = sh(["docker", "inspect", name,
                  "--format", "{{.Created}}"]).strip()
    return {"image_id": iid[:19], "created": created[:10]}


def ports() -> list[str]:
    out = sh(["ss", "-tlnp"])
    return [ln.split()[3] for ln in out.splitlines()
            if ln.startswith("LISTEN") and any(
                f":{p}" in ln for p in ("8123", "8095", "8100", "8200", "8300"))]


def nas(expected_source: str, mount_point: str) -> dict:
    mounts = Path("/proc/mounts").read_text(encoding="utf-8")
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == mount_point:
            return {"mounted": True, "source": parts[0],
                    "matches_expected":
                        (not expected_source) or (expected_source in parts[0])}
    return {"mounted": False}


def env_keys() -> list[str]:
    keys = []
    env = APP_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.append(line.split("=", 1)[0])
    return keys


def control_db() -> dict:
    db = APP_DIR / "data" / "control" / "control.db"
    if not db.exists():
        return {"exists": False}
    import sqlite3
    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    return {"exists": True, "tables": sorted(tables)}


def stt_models() -> list[str]:
    d = APP_DIR / "data" / "models" / "sense-voice"
    if not d.exists():
        return []
    return [p.name for p in d.iterdir() if p.suffix in (".onnx", ".txt")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report: dict = {"generated_for": "IMP-04 preflight",
                    "app_dir": str(APP_DIR)}
    report["containers"] = containers()
    report["images"] = {c["name"]: image_info(c["name"])
                        for c in report["containers"]}
    report["ports"] = ports()
    report["nas"] = nas("192.168.1.10", "/mnt/music")   # 占位；现场按实际填
    keys = env_keys()
    report["env"] = {"keys_present": keys,
                     "missing_vs_expected":
                         [k for k in EXPECTED_ENV_KEYS if k not in keys]}
    report["control_db"] = control_db()
    report["stt_models"] = stt_models()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
