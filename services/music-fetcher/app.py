#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""music-fetcher —— 按需补库服务（生产部署版）。

把「本地库没有的歌 → musicdl 补 → MA 重扫 → 经 HA 播放」做成常驻服务，
供 HA rest_command / voice-gateway 触发。

设计约束：
  * **单工作者串行**：下载是重活，musicdl 又脆，并发只会互相拖死。
  * **作业异步**：POST /backfill 立即返回 job_id，下载在后台跑（一次 1~3 分钟）。
  * **同质去重**：同一 query 已有排队/运行中的作业时，直接复用，不重复下载。
  * **播放经 HA**：成功的播放动作走 `script.music_play_uri`（D13），
    HA 挂了只告警，不影响入库结果。

API：
  GET  /health              健康与配置自检
  POST /backfill            提交补库作业（202 + job_id）
  GET  /jobs/{job_id}       查询单个作业
  GET  /jobs                最近作业列表
  POST /jobs/{job_id}/cancel 尽力取消（标记；已启动的只能等它跑完）
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

# ---- 网络硬化：IPv6 黑洞 + 无 timeout 请求（与 musicdl_fetch.py 同源）----
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    infos = _orig_getaddrinfo(host, port, family, type, proto, flags)
    if family in (0, _socket.AF_UNSPEC) and len(infos) > 1:
        infos = sorted(infos, key=lambda i: 0 if i[0] == _socket.AF_INET else 1)
    return infos


_socket.getaddrinfo = _ipv4_first
_socket.setdefaulttimeout(30)

import requests as _requests  # noqa: E402

_req_orig = _requests.sessions.Session.request


def _request_with_timeout(self, method, url, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = (10, 30)
    return _req_orig(self, method, url, **kwargs)


_requests.sessions.Session.request = _request_with_timeout

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

# ---------------------------------------------------------------- 配置
ENV_PATH = os.environ.get("ENV_PATH", "/opt/home-music-agent/.env")


def load_env_file(path: str):
    """读 .env（不覆盖已有环境变量；值里的 # 不做注释处理）。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


load_env_file(ENV_PATH)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(APP_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_LOG = DATA_DIR / "jobs.jsonl"
ENRICH_LOG = DATA_DIR / "enrich_tasks.jsonl"

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
AUTO_BACKFILL = Path(os.environ.get("AUTO_BACKFILL", str(APP_DIR / "auto_backfill.py")))
# 推荐器：MA 自带 music/recommendations 实测 19 行 0 条，等于没有推荐能力，
# 这层用「本地口味 + 在线聚合目录」补上（见 recommend.py 文档）。
RECOMMEND = Path(os.environ.get("RECOMMEND", str(APP_DIR / "recommend.py")))
ENRICH_PLAN = Path(os.environ.get("ENRICH_PLAN", str(APP_DIR / "enrich_plan.py")))
PY = os.environ.get("MUSICDL_PY", sys.executable)
# 音源组合（2026-09-13 实测「周杰伦 稻香」原唱命中率）：
#   JBSouMusicClient 10/10 ★ / MituMusicClient 5/6 / NeteaseMusicClient 1/10
# 音源越多越慢（JBSou 单次约 100s），按需增减。
DEFAULT_SOURCES = [s for s in os.environ.get(
    "SOURCES", "JBSouMusicClient,MituMusicClient,NeteaseMusicClient").split(",") if s]
# 一次补几条**不同作品**：1 = 只补点播的那一首（语音兜底走这条，
# 且 pick=1 时「本地已有」能正常短路，不会白跑）。
# >1 = 「充实曲库」意图，由显式调用方传（如 fetcher_job.py --pick 3），
# 此时会跳过「本地已有同歌手歌」的短路，改为逐候选查库去重后补 N 首。
DEFAULT_PICK = int(os.environ.get("PICK", "1"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "1800"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "200"))

# --- 每日充实异步化（2026-09-14）------------------------------------------
# 背景：HA 的 rest_command 实测 300s 上限，而「画像选题 ~90s + 每个口味种子一次
#   musicdl 搜索（最坏 240s/次）」实测 390s ~ 570s ——
#   同步接口必被 HA 掐断，表现为自动化报错、完成通知丢失（下载其实照跑：
#   2026-09-14 首次真实运行就是 11:30 触发、11:37~11:42 三首入库、HA 报
#   "Timeout when calling resource .../enrich"）。
# 对策：/enrich/submit 立即返回 202 + task_id，后台线程干完活回调 HA 通知。
HA_NOTIFY_URL = os.environ.get("HA_NOTIFY_URL", "http://127.0.0.1:8123").rstrip("/")
HA_NOTIFY_TOKEN = (os.environ.get("HA_TOKEN")
                   or os.environ.get("HA_ACCESS_TOKEN") or "")
ENRICH_TASK_KEEP = int(os.environ.get("ENRICH_TASK_KEEP", "50"))

app = FastAPI(title="music-fetcher", version="1.0.0")
logger = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------- 作业存储
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_q: "queue.Queue[str]" = queue.Queue()
_cancel: set[str] = set()
# 异步充实任务（2026-09-14）：/enrich/submit 的进度与结果，见该路由注释
_tasks: dict[str, dict] = {}


def _now():
    return time.time()


def _persist(job: dict):
    try:
        with JOBS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _tail(text: str, n=30):
    lines = [l for l in str(text or "").splitlines() if l.strip()]
    return lines[-n:]


def _notify_ha(title: str, message: str) -> bool:
    """往 HA 发 persistent_notification —— 异步任务唯一可见的回执渠道。

    ⚠️ 只做「通知」，不做音乐动作（D13：音乐动作必须由 HA 下发）。
    """
    if not HA_NOTIFY_TOKEN:
        return False
    try:
        body = json.dumps({"title": title, "message": message}).encode("utf-8")
        req = urllib.request.Request(
            HA_NOTIFY_URL + "/api/services/persistent_notification/create",
            data=body,
            headers={"Authorization": "Bearer " + HA_NOTIFY_TOKEN,
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:                              # noqa: BLE001
        logger.warning("HA 通知失败：%s: %s", type(e).__name__, e)
        return False


def _ha_call(path: str, payload: dict, timeout: int = 10) -> bool:
    """通用 HA REST 调用（只用于回执/状态上报；音乐动作仍由 HA 脚本下发）。"""
    if not HA_NOTIFY_TOKEN:
        logger.warning("HA 令牌缺失，跳过 %s", path)
        return False
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            HA_NOTIFY_URL + path, data=body,
            headers={"Authorization": "Bearer " + HA_NOTIFY_TOKEN,
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as e:                              # noqa: BLE001
        logger.warning("HA 调用失败 %s：%s: %s", path, type(e).__name__, e)
        return False


# ⚠️ 2026-09-14 实测：本机 HA 的 persistent_notification.create 返回 []，
#   但 websocket persistent_notification/get 恒为 0 条 —— 通知「建了读不到」，
#   不能作为唯一回执通道。故回执另写一份到 **状态实体**：
#   input_text.music_enrich_last（HA UI 可见、API 可查、PWA 后续可读）。
ENRICH_STATE_ENTITY = os.environ.get("ENRICH_STATE_ENTITY",
                                     "input_text.music_enrich_last")


def _set_ha_state(value: str) -> bool:
    """把最近一次充实的结论写进 HA 状态实体（回执的可靠通道）。"""
    return _ha_call("/api/services/input_text/set_value",
                    {"entity_id": ENRICH_STATE_ENTITY,
                     "value": str(value)[:255]})


def _record_task(task: dict):
    """把任务终态追加到 data/enrich_tasks.jsonl，供复核与后续分析。"""
    if not task:
        return
    try:
        row = {k: task.get(k) for k in
               ("task_id", "status", "created", "updated", "submitted",
                "elapsed", "error")}
        row["summary"] = ((task.get("result") or {}).get("plan") or {}).get("summary")
        row["finished"] = [{"query": f.get("query"), "ok": f.get("ok"),
                            "name": f.get("name"),
                            "reason": f.get("reason") or f.get("error")}
                           for f in (task.get("finished") or [])]
        with ENRICH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:                              # noqa: BLE001
        logger.warning("写 enrich_tasks.jsonl 失败：%s", e)


def _task_update(tid: str, **kw):
    with _lock:
        t = _tasks.get(tid)
        if not t:
            return None
        t.update(kw)
        t["updated"] = _now()
        return dict(t)


def _prune_tasks():
    """只保留最近 ENRICH_TASK_KEEP 个任务（内存可控，仍够排查）。"""
    if len(_tasks) <= ENRICH_TASK_KEEP:
        return
    for tid in sorted(_tasks, key=lambda k: _tasks[k]["created"])[:-ENRICH_TASK_KEEP]:
        _tasks.pop(tid, None)


_JOB_TERMINAL = ("done", "rejected", "failed", "cancelled")


def _job_summary(job_id: str) -> dict:
    with _lock:
        j = _jobs.get(job_id) or {}
    res = j.get("result") or {}
    track = res.get("track") or {}
    return {"job_id": job_id, "query": j.get("query"), "status": j.get("status"),
            "ok": bool(res.get("ok")), "reason": res.get("reason"),
            "name": track.get("name"), "error": j.get("error")}


def _wait_jobs(job_ids: "list[str]", timeout: "int | None" = None) -> "list[dict]":
    """等一批作业进入终态。跑在后台线程里，不占 HTTP 连接。"""
    limit = time.time() + (timeout or JOB_TIMEOUT)
    while time.time() < limit:
        with _lock:
            states = [(_jobs.get(j) or {}).get("status") for j in job_ids]
        if all(s in _JOB_TERMINAL for s in states):
            break
        time.sleep(5)
    return [_job_summary(j) for j in job_ids]


def _make_job(query: str, req: "BackfillReq") -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "query": query,
        "status": "queued",
        "created": _now(),
        "updated": _now(),
        "options": {
            "sources": req.sources or DEFAULT_SOURCES,
            "out": req.out or MUSIC_DIR,
            "min_duration": req.min_duration,
            "pick": req.pick if req.pick is not None else DEFAULT_PICK,
            "play": False,                # IMP-02：自动播放已阻断
            "play_blocked": bool(req.play),
            "dry_run": bool(req.dry_run),
        },
        "result": None,
        "log": [],
        "error": None,
    }


def _run_job(job: dict):
    jid = job["id"]
    job["status"] = "running"
    job["updated"] = _now()

    if jid in _cancel:
        job.update(status="cancelled", updated=_now())
        _persist(job)
        return

    opt = job["options"]
    cmd = [PY, str(AUTO_BACKFILL), job["query"], "--json",
           "--sources", *opt["sources"],
           "--out", opt["out"],
           "--min-duration", str(opt["min_duration"]),
           "--pick", str(opt.get("pick") or DEFAULT_PICK),
           "--timeout", str(JOB_TIMEOUT)]
    if opt.get("play"):
        # IMP-02：自动播放路径已阻断——play=true 不再转发给 CLI。
        job["play_blocked"] = True
        log("  [IMP-02] 作业携带 play=true，已被阻断（仅入库）")
    if opt["dry_run"]:
        cmd.append("--dry-run")

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=JOB_TIMEOUT + 60)
        stdout = (p.stdout or "").strip()
        stderr = p.stderr or ""
        result = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    result = json.loads(line)
                    break
                except Exception:
                    continue
        job["log"] = _tail(stderr)
        if result is None:
            job.update(status="failed", error="no_json_output",
                       log=job["log"] + _tail(stdout, 20), updated=_now())
        else:
            job["result"] = result
            if result.get("ok"):
                st = "done"
            elif result.get("reason") in ("all_candidates_rejected", "duration_too_short",
                                          "no_online_candidates", "no_file_produced"):
                st = "rejected"
            else:
                st = "failed"
            job.update(status=st, error=result.get("reason"), updated=_now())
    except subprocess.TimeoutExpired:
        job.update(status="failed", error="timeout", updated=_now())
    except Exception as e:
        job.update(status="failed", error="%s: %s" % (type(e).__name__, e), updated=_now())
    _persist(job)


def _worker():
    while True:
        jid = _q.get()
        with _lock:
            job = _jobs.get(jid)
        if not job:
            _q.task_done()
            continue
        try:
            _run_job(job)
        finally:
            _q.task_done()


threading.Thread(target=_worker, daemon=True).start()


def _enqueue(job: dict):
    with _lock:
        _jobs[job["id"]] = job
        # 内存里只留最近 MAX_JOBS 条
        if len(_jobs) > MAX_JOBS:
            for k in sorted(_jobs, key=lambda x: _jobs[x]["created"])[:len(_jobs) - MAX_JOBS]:
                _jobs.pop(k, None)
    _persist(job)
    _q.put(job["id"])
    return job


# ---------------------------------------------------------------- API 模型
class BackfillReq(BaseModel):
    query: str = Field(..., description='查询词，如 "周杰伦 晴天"')
    sources: list[str] | None = Field(None, description="musicdl 音源")
    out: str | None = Field(None, description="落盘目录，默认 /music")
    min_duration: int = Field(60, ge=10, description="最短可接受时长（秒）")
    pick: int | None = Field(None, ge=1, le=20,
                             description="下载几条不同作品（丰富曲库）；默认取服务端 PICK")
    play: bool = Field(False, description=(
        "已弃用（IMP-02）：补库完成只入库不播放；此字段仅保留兼容，"
        "传 true 也会被忽略"))
    dry_run: bool = Field(False, description="只决策不下载")


class RecommendReq(BaseModel):
    seed: str | None = Field(None, description='种子：歌手名或场景词；不给则 --auto')
    auto: bool = Field(True, description="不给 seed 时，自动取库里曲目最多的歌手")
    limit: int = Field(10, ge=1, le=50, description="推荐条数")
    max_size_mb: int = Field(0, ge=0, description="剔除超大候选（0=不限）。"
                                                  "聚合音源 flac 常见 100~200MB/首")
    sources: list[str] | None = Field(None, description="musicdl 音源")


class EnrichReq(BaseModel):
    seed: str | None = Field(None, description="种子；不给则按本地口味自动推导")
    n: int = Field(3, ge=1, le=20, description="把前 N 条推荐提交补库")
    limit: int = Field(20, ge=1, le=50, description="先生成多少条候选再取前 N")
    max_size_mb: int = Field(0, ge=0, description="剔除超大候选（0=不限）")
    play: bool = Field(False, description="补库成功后播放（默认否）")
    sources: list[str] | None = Field(None, description="musicdl 音源")


# ---------------------------------------------------------------- 路由
@app.get("/health")
def health():
    music_dir = Path(MUSIC_DIR)
    musicdl_ok = AUTO_BACKFILL.exists()
    try:
        import musicdl  # noqa: F401
        ver = getattr(musicdl, "__version__", "unknown")
    except Exception:
        ver = "not-importable"
    return {
        "ok": True,
        "service": "music-fetcher",
        "version": "1.0.0",
        "musicdl": ver,
        "music_dir": MUSIC_DIR,
        "music_dir_exists": music_dir.exists(),
        "music_dir_writable": os.access(str(music_dir), os.W_OK) if music_dir.exists() else False,
        "auto_backfill": str(AUTO_BACKFILL),
        "auto_backfill_ok": musicdl_ok,
        "recommend": str(RECOMMEND),
        "recommend_ok": RECOMMEND.exists(),
        "sources": DEFAULT_SOURCES,
        "pick": DEFAULT_PICK,
        "queued": _q.qsize(),
        "jobs": len(_jobs),
        "enrich_mode": "async",
        "enrich_tasks": len(_tasks),
        "enrich_state_entity": ENRICH_STATE_ENTITY,
        "ha_notify": bool(HA_NOTIFY_TOKEN),
        "ma_token": bool(os.environ.get("MA_TOKEN") or os.environ.get("MA_LONG_TOKEN")),
        "ha_token": bool(os.environ.get("HA_TOKEN") or os.environ.get("HA_ACCESS_TOKEN")
                         or os.environ.get("HA_REFRESH_TOKEN")),
    }


@app.post("/backfill", status_code=202)
def backfill(req: BackfillReq):
    query = re.sub(r"\s+", " ", str(req.query or "").strip())
    if not query:
        raise HTTPException(400, "query must not be empty")

    # 同质去重：同 query 且状态为 queued/running 的，直接复用
    with _lock:
        for j in _jobs.values():
            if j["query"] == query and j["status"] in ("queued", "running"):
                return {"job_id": j["id"], "status": j["status"], "reused": True}
    job = _enqueue(_make_job(query, req))
    return {"job_id": job["id"], "status": "queued", "reused": False}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/jobs")
def list_jobs(limit: int = 20):
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
    return jobs[:limit]


# ---------------------------------------------------------------- 推荐
def _run_recommend(req: "RecommendReq") -> dict:
    """跑 recommend.py --json，返回它 stdout 里的那行 JSON。"""
    if not RECOMMEND.exists():
        raise HTTPException(503, "recommend.py 未部署：%s" % RECOMMEND)
    cmd = [PY, str(RECOMMEND), "--json",
           "--limit", str(req.limit), "--max-size-mb", str(req.max_size_mb)]
    if req.seed and req.seed.strip():
        cmd += ["--seed", req.seed.strip()]
    else:
        cmd += ["--auto"]
    if req.sources:
        cmd += ["--sources", *req.sources]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "recommend 超时（%ss）" % JOB_TIMEOUT)
    for line in reversed((p.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:                       # noqa: BLE001
                continue
    raise HTTPException(502, {"error": "no_json_output", "log": _tail(p.stderr, 20)})


@app.post("/recommend")
def recommend(req: RecommendReq):
    """推荐「库里还没有」的曲目。不下载，只给清单。"""
    return _run_recommend(req)


def _submit_query(q: str, req: "EnrichReq",
                  extra: dict | None = None) -> dict:
    job = _enqueue(_make_job(
        q, BackfillReq(query=q, play=bool(req.play), pick=1,
                       sources=req.sources)))
    out = {"query": q, "job_id": job["id"], "status": job["status"]}
    if extra:
        out.update(extra)
    return out


def _top_recommend_for_seed(seed: str, req: "EnrichReq") -> dict | None:
    """给一个种子歌手，搜一圈返回第一条可用推荐（复用 recommend.py）。"""
    recs = _run_recommend(RecommendReq(
        seed=seed, auto=False, limit=req.limit,
        max_size_mb=req.max_size_mb, sources=req.sources))
    items = [c for c in (recs.get("recommendations") or []) if c.get("song_name")]
    if not items:
        return None
    c = items[0]
    artist = re.split(r"[/&、,，;；]", str(c.get("singers") or ""))[0].strip()
    return {"query": ("%s %s" % (artist, c.get("song_name"))).strip(),
            "song_name": c.get("song_name"), "singers": c.get("singers"),
            "ext": c.get("ext"), "dur": c.get("dur")}


@app.post("/enrich")
def enrich(req: EnrichReq):
    """推荐 + 直接把前 N 条提交补库（同步版）。

    v2（画像版，2026-09-13）：不给 seed 时走 enrich_plan.py 选题——
      口味池（taste.py 画像加权随机）+ 探索池（网易榜单/内置热门池），
      防「永远只推库里最多歌手」的信息茧房。给显式 seed 时保持 v1 行为。

    ⚠️ 本接口会阻塞到「作业提交完成」（实测 390s~570s），只适合脚本/人工调试。
       HA 侧自动化请用 /enrich/submit（异步，见下）。
    """
    return _enrich_run(req)


def _enrich_single_seed(req: EnrichReq):
    """v1 单种子路径（显式 seed / 向后兼容）。"""
    recs = _run_recommend(RecommendReq(
        seed=req.seed, auto=True, limit=req.limit,
        max_size_mb=req.max_size_mb, sources=req.sources))
    items = [c for c in (recs.get("recommendations") or []) if c.get("song_name")]

    jobs = []
    for c in items[: req.n]:
        # 查询词取「第一位歌手 + 歌名」：候选里常是「蔡依林, 周杰伦」这种多人名，
        # 直接拼进去会变成很糟的查询词
        artist = re.split(r"[/&、,，;；]", str(c.get("singers") or ""))[0].strip()
        q = ("%s %s" % (artist, c.get("song_name"))).strip()
        if not q:
            continue
        jobs.append(_submit_query(q, req,
                                  {"song_name": c.get("song_name"),
                                   "singers": c.get("singers")}))

    return {"mode": "single_seed", "seed": recs.get("seed"),
            "seed_from": recs.get("seed_from"),
            "library_size": recs.get("library_size"),
            "recommendations": items[: req.n], "jobs": jobs,
            "submitted": len(jobs)}


def _build_plan(req: EnrichReq) -> dict:
    """跑 enrich_plan.py 拿本期选题（口味池 + 探索池）—— 慢，约 90s。"""
    if not ENRICH_PLAN.exists():
        raise HTTPException(503, "enrich_plan.py 未部署：%s" % ENRICH_PLAN)
    cmd = [PY, str(ENRICH_PLAN), "--json", "--n", str(req.n)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "enrich_plan 超时（90s）")
    plan = None
    for line in reversed((p.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                plan = json.loads(line)
                break
            except Exception:                       # noqa: BLE001
                continue
    if plan is None:
        raise HTTPException(502, {"error": "no_plan_output",
                                  "log": _tail(p.stderr, 20)})
    if not plan.get("ok"):
        raise HTTPException(502, {"error": "plan_failed",
                                  "detail": plan})
    return plan


def _submit_plan_jobs(plan: dict, req: EnrichReq) -> "list[dict]":
    """按选题提交补库作业 —— 每个口味种子一次 musicdl 搜索（最坏 240s/次）。"""
    jobs = []

    # ---- 口味池：每个种子歌手取 1 首推荐
    for s in plan.get("taste_seeds") or []:
        if len(jobs) >= req.n:
            break
        try:
            pick = _top_recommend_for_seed(s["name"], req)
        except HTTPException:
            pick = None
        if pick:
            jobs.append(_submit_query(pick["query"], req, {
                "pool": "taste", "seed": s["name"],
                "reason": s.get("reason"),
                "song_name": pick["song_name"], "singers": pick["singers"]}))

    # ---- 探索池：榜单歌曲直发（具体歌名无需再搜推荐）
    chart = plan.get("chart_seed") or {}
    if len(jobs) < req.n and chart:
        if chart.get("kind") == "song":
            q = ("%s %s" % (chart.get("singers"), chart.get("song_name"))).strip()
            jobs.append(_submit_query(q, req, {
                "pool": "explore", "source": chart.get("source"),
                "song_name": chart.get("song_name"),
                "singers": chart.get("singers")}))
        elif chart.get("kind") == "artist" and len(jobs) < req.n:
            try:
                pick = _top_recommend_for_seed(chart["seed"], req)
            except HTTPException:
                pick = None
            if pick:
                jobs.append(_submit_query(pick["query"], req, {
                    "pool": "explore", "source": chart.get("source"),
                    "song_name": pick["song_name"],
                    "singers": pick["singers"]}))
    return jobs


def _enrich_profile(req: EnrichReq):
    """v2 画像路径：口味池 + 探索池（同步版）。"""
    plan = _build_plan(req)
    jobs = _submit_plan_jobs(plan, req)
    return {"mode": "profile_v2", "summary": plan.get("summary"),
            "plan": plan, "jobs": jobs, "submitted": len(jobs)}


def _enrich_run(req: EnrichReq) -> dict:
    """同步/异步两条路共用的执行体。"""
    if req.seed and req.seed.strip():
        return _enrich_single_seed(req)
    return _enrich_profile(req)


# ---------------------------------------------------------------- 充实回执文案
def _plan_lines(res: dict) -> "list[str]":
    """把选题翻译成人话（进 HA 通知）。"""
    plan = res.get("plan") or {}
    out = []
    for s in plan.get("taste_seeds") or []:
        out.append("· 口味：%s（%s）" % (s.get("name"), s.get("reason") or "画像"))
    ch = plan.get("chart_seed") or {}
    if ch:
        who = ch.get("song_name") or ch.get("seed") or ch.get("name") or "榜单曲"
        out.append("· 探索：%s（%s）" % (who, ch.get("source") or "榜单"))
    if not out and res.get("seed"):
        out.append("· 种子：%s" % res.get("seed"))
    return out


def _submitted_message(res: dict) -> str:
    jobs = res.get("jobs") or []
    lines = ["已提交 %d 首，后台下载中（约 2~4 分钟/首，不打断播放）。" % len(jobs)]
    pick = _plan_lines(res)
    if pick:
        lines.append("选题：")
        lines.extend(pick)
    for j in jobs[:5]:
        lines.append("→ %s%s" % (j.get("song_name") or j.get("query"),
                                 "（%s）" % j.get("seed") if j.get("seed") else ""))
    return "\n".join(lines)


def _done_message(res: dict, finished: "list[dict]", elapsed: float) -> str:
    ok = [f for f in finished if f.get("ok")]
    bad = [f for f in finished if not f.get("ok")]
    lines = ["%d/%d 首已入库，用时 %.0f 秒。" % (len(ok), len(finished), elapsed)]
    for f in ok:
        lines.append("✅ %s" % (f.get("query") or f.get("name") or ""))
    for f in bad:
        lines.append("❌ %s（%s）" % (f.get("query") or "",
                                     f.get("reason") or f.get("error")
                                     or f.get("status") or "失败"))
    lines.append("（MA 扫描后点播即命中本地完整版）")
    return "\n".join(lines)


def _run_enrich_task(tid: str, req: EnrichReq):
    """后台线程：选题 → 提交 → 等入库 → 回调 HA 通知。"""
    t0 = _now()
    try:
        res = _enrich_run(req)
    except HTTPException as e:
        _task_update(tid, status="error", error=str(e.detail),
                     elapsed=round(_now() - t0, 1))
        _notify_ha("曲库充实失败", "选题/推荐阶段失败：%s" % e.detail)
        _set_ha_state("失败：选题/推荐阶段 %s" % str(e.detail)[:180])
        _record_task(_tasks.get(tid) or {})
        return
    except Exception as e:                              # noqa: BLE001
        _task_update(tid, status="error", error=repr(e),
                     elapsed=round(_now() - t0, 1))
        _notify_ha("曲库充实失败", repr(e))
        _set_ha_state("失败：%s" % repr(e)[:180])
        _record_task(_tasks.get(tid) or {})
        return

    jobs = res.get("jobs") or []
    ids = [j["job_id"] for j in jobs]
    _task_update(tid, status="downloading", result=res, jobs=jobs,
                 submitted=len(jobs), elapsed=round(_now() - t0, 1))
    _notify_ha("曲库充实：已选题", _submitted_message(res))
    if not ids:
        _task_update(tid, status="done", finished=[],
                     elapsed=round(_now() - t0, 1))
        _notify_ha("曲库充实：完成", "本期没有提交任何作业（推荐为空或全被过滤）。")
        _set_ha_state("已完成，但本期没有提交任何作业（推荐为空或全被过滤）")
        _record_task(_tasks.get(tid) or {})
        return

    finished = _wait_jobs(ids)
    _task_update(tid, status="done", finished=finished,
                 elapsed=round(_now() - t0, 1))
    _notify_ha("曲库充实：完成", _done_message(res, finished, _now() - t0))
    ok = [f for f in finished if f.get("ok")]
    _set_ha_state("入库 %d/%d（%.0f 秒）：%s" % (
        len(ok), len(finished), _now() - t0,
        "、".join((f.get("name") or f.get("query") or "?") for f in ok) or "无"))
    _record_task(_tasks.get(tid) or {})


@app.post("/enrich/submit", status_code=202)
def enrich_submit(req: EnrichReq):
    """异步充实：立即返回 task_id，选题/推荐/提交/等待都在后台线程。

    HA 侧用 rest_command 调本接口（timeout 只需几十秒即可），完成后由本服务
    回调 HA persistent_notification 给回执（两条：已选题 / 已完成）。
    进度查询：GET /enrich/tasks/{task_id}。
    """
    tid = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[tid] = {"task_id": tid, "status": "running", "created": _now(),
                       "updated": _now(), "n": req.n, "play": bool(req.play),
                       "submitted": 0, "jobs": [], "finished": [],
                       "result": None, "error": None}
        _prune_tasks()
    threading.Thread(target=_run_enrich_task, args=(tid, req),
                     daemon=True).start()
    return {"accepted": True, "task_id": tid, "n": req.n}


@app.get("/enrich/tasks/{task_id}")
def enrich_task(task_id: str):
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            raise HTTPException(404, "task not found")
        return dict(t)


@app.get("/enrich/tasks")
def enrich_tasks(limit: int = 10):
    with _lock:
        ts = sorted(_tasks.values(), key=lambda t: t["created"], reverse=True)
        return [dict(t) for t in ts[:limit]]


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _cancel.add(job_id)
    if job["status"] == "queued":
        job.update(status="cancelled", updated=_now())
        _persist(job)
    return {"job_id": job_id, "status": job["status"], "cancel_requested": True}
