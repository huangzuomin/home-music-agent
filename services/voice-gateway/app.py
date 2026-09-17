#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Home Music Agent MVP v0.2 — Voice Gateway（Phase 4 + Phase 5）

职责（严格守住方案 §9.2 的「薄客户端」原则）：
    STT 文本 → 意图解析 → 调用 Home Assistant script → MA → 播放器

两段式意图解析（Phase 5 新增）：
    1. 【快路径】规则匹配。命中即返回，耗时 <1ms，覆盖所有高频硬指令
       （下一首 / 暂停 / 大声点 / 放周杰伦）。
    2. 【慢路径】GLM-4-Flash function calling。只处理规则覆盖不了的
       自然语言场景（"来点适合写代码的安静音乐"、"太闹了换点舒缓的"）。
       失败/超时自动降级为 unknown，绝不阻塞语音链路。

设计约束（D13：严格经 HA 编排）：
    * 本服务**不直连 Music Assistant**。所有音乐动作都必须经 HA script.turn_on 下发。
    * LLM 只产出「意图 + 参数」，不直接执行任何动作。

对外接口：
    POST /command  {"text": "播放周杰伦", "mode": "auto|rules|agent"}
        → {"intent":..., "script":..., "ok":bool, "path":"rules"|"agent"}
    POST /tts      {"text": "已切到下一首", "voice": "..."} → audio/mpeg
    GET  /health   → 服务与依赖状态
    GET  /intents  → 规则表（调试）
    GET  /tools    → LLM 工具表（调试）
    GET  /tts/voices → 可用中文音色

用法：uvicorn app:app --host 0.0.0.0 --port 8200
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
import urllib.error
import uuid
import urllib.parse
import urllib.request
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel

import llm as llm_mod
import tools as tools_mod
import tts as tts_mod
import agent as agent_mod
import context as context_mod
import session as session_mod
import contracts
import ha_executor as ha_executor_mod
import coordinator as coordinator_mod
import storage as storage_mod
import devices as devices_mod
import auth as auth_mod
import health as health_mod
import playback_state as playback_state_mod
import scene_catalog as scene_catalog_mod
import api_v1 as api_v1_mod
import scene_catalog as scene_catalog_mod

HA_BASE = os.environ.get("HA_BASE", "http://127.0.0.1:8123")
ENV_PATH = pathlib.Path(os.environ.get("ENV_PATH", "/opt/home-music-agent/.env"))
LOG_PATH = pathlib.Path(os.environ.get("LOG_PATH", "/app/data/sessions/voice-gateway.jsonl"))
AGENT_ENABLED = os.environ.get("AGENT_ENABLED", "true").lower() in ("1", "true", "yes")
AGENT_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", "12"))
# v0.3：Smart Path 的**硬超时**（秒）。超过即放弃 LLM、降级规则路径。
# ⚠️ 为什么必须有（2026-09-13 实测）：智谱 API 单次请求约 2.4s 正常，
#    但连续请求会触发排队，同一形态请求劣化到 25s、甚至 181s。
#    语音交互绝不能等这么久 —— 宁可降级到规则，也不能卡住用户。
#    必须小于 AGENT_TIMEOUT（socket 级超时），否则永远轮不到它生效。
AGENT_HARD_TIMEOUT = float(os.environ.get("AGENT_HARD_TIMEOUT", "8"))
TTS_ENABLED = os.environ.get("TTS_ENABLED", "true").lower() in ("1", "true", "yes")
TTS_VOICE = os.environ.get("TTS_VOICE", tts_mod.DEFAULT_VOICE)
# v0.3：/agent（Session 化 Agent）。false 时 /agent 自动降级为旧 /command 逻辑。
AGENT_SESSION_ENABLED = os.environ.get("AGENT_SESSION_ENABLED", "true").lower() in ("1", "true", "yes")
# IMP-03c：控制核心开关。false 时回到 IMP-02 行为（直接 HA script，安全默认保留）。
CONTROL_CORE_V1 = os.environ.get("CONTROL_CORE_V1", "true").lower() in ("1", "true", "yes")
# IMP-04：设备鉴权与限速。部署切换窗口置 true（cutover 检查单 §0）。
DEVICE_AUTH_REQUIRED = os.environ.get("DEVICE_AUTH_REQUIRED", "false").lower() in ("1", "true", "yes")
ALLOWED_ORIGINS = [h for h in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1").split(",") if h]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
STT_BASE = os.environ.get("STT_BASE", "http://127.0.0.1:8100")
NAS_EXPECTED_SOURCE = os.environ.get("NAS_EXPECTED_SOURCE", "")
NAS_MOUNT_POINT = os.environ.get("NAS_MOUNT_POINT", "/mnt/music")
SCENES_FILE = os.environ.get("SCENES_FILE", "config/scenes.yaml")
CONTEXT_TRACKS = int(os.environ.get("CONTEXT_TRACKS", "20"))
CONVERSATION_TURNS = int(os.environ.get("CONVERSATION_TURNS", "10"))

app = FastAPI(title="Home Music Agent — Voice Gateway (Phase 4+5)")


# ------------------------------------------------------------------ 凭据

def _env() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not ENV_PATH.exists():
        return cfg
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


_CFG = _env()

_llm: llm_mod.LLMClient | None = None


def get_llm() -> llm_mod.LLMClient:
    """懒加载 LLM 客户端。配置文件变更后重启容器即可生效。

    变量名通用化：LLM_* 优先，兼容历史 ZHIPU_*。
    （2026-09-13 由智谱切到 DeepSeek：智谱 DNS 含不通的 IPv6 导致每次请求
    白等 40s；DeepSeek 纯 IPv4 且 flash 响应约 1s。保留旧变量名只为不破坏
    既有 .env，新部署直接用 LLM_*。）
    """
    global _llm
    if _llm is None:
        api_key = _CFG.get("LLM_API_KEY") or _CFG.get("ZHIPU_API_KEY", "")
        base_url = (_CFG.get("LLM_BASE") or _CFG.get("ZHIPU_BASE")
                    or "https://api.deepseek.com/v1")
        model = (_CFG.get("LLM_MODEL") or _CFG.get("ZHIPU_MODEL")
                 or "deepseek-flash")
        # DeepSeek V4 是推理模型，默认先生成思考 token。实测（2026-09-13）：
        #   带思考：1.8~2.9s / 输出 262~554 token，且 max_tokens 小时 tool_call
        #           会被截断成半截 JSON（`{"scene": "work"` 后面直接断掉）
        #   关思考：0.66~1.07s / 输出 9~140 token，行为同样正确
        # 故默认关闭；换回非推理模型时该字段会被忽略，不影响兼容性。
        extra: dict[str, Any] = {}
        if _CFG.get("LLM_DISABLE_THINKING", "true").lower() in ("1", "true", "yes"):
            extra["thinking"] = {"type": "disabled"}
        _llm = llm_mod.LLMClient(api_key=api_key, base_url=base_url,
                                 model=model, timeout=AGENT_TIMEOUT,
                                 extra_payload=extra)
    return _llm


class HAClient:
    """HA REST 客户端。access token 30 分钟过期，这里用 refresh token 自动续期。"""

    def __init__(self) -> None:
        self._access = ""
        self._lock_at = 0.0

    def _token(self) -> str:
        now = time.time()
        if self._access and now - self._lock_at < 1500:      # 25 分钟
            return self._access
        cfg = _env()
        refresh = cfg.get("HA_REFRESH_TOKEN")
        if refresh:
            try:
                body = urllib.parse.urlencode({
                    "client_id": HA_BASE + "/", "grant_type": "refresh_token",
                    "refresh_token": refresh,
                }).encode()
                req = urllib.request.Request(HA_BASE + "/auth/token", data=body, method="POST")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
                with urllib.request.urlopen(req, timeout=10) as r:
                    self._access = json.loads(r.read().decode())["access_token"]
                    self._lock_at = now
                    return self._access
            except Exception:
                pass
        self._access = cfg.get("HA_ACCESS_TOKEN", "")
        self._lock_at = now
        return self._access

    def call_script(self, name: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps({"entity_id": "script." + name, "variables": variables or {}}).encode()
        req = urllib.request.Request(HA_BASE + "/api/services/script/turn_on",
                                     data=body, method="POST")
        req.add_header("Authorization", "Bearer " + self._token())
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return {"ok": True, "status": r.status}
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code, "detail": e.read().decode()[:200]}
        except Exception as e:
            return {"ok": False, "status": 0, "detail": str(e)[:200]}

    def state(self, entity_id: str) -> str:
        req = urllib.request.Request("%s/api/states/%s" % (HA_BASE, entity_id))
        req.add_header("Authorization", "Bearer " + self._token())
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode()).get("state", "")
        except Exception:
            return ""

    def state_full(self, entity_id: str) -> dict[str, Any]:
        req = urllib.request.Request("%s/api/states/%s" % (HA_BASE, entity_id))
        req.add_header("Authorization", "Bearer " + self._token())
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode())
        except Exception:
            return {}


ha = HAClient()


# ------------------------------------------------------------------ v0.3 单例

_sessions: session_mod.SessionStore | None = None
_ma: context_mod.MAClient | None = None
_history: context_mod.TrackHistory | None = None
_control_store: storage_mod.ControlStore | None = None
_coordinator: coordinator_mod.CommandCoordinator | None = None
_device_auth: auth_mod.DeviceAuth | None = None
_health_checker: health_mod.HealthChecker | None = None


def get_device_auth() -> auth_mod.DeviceAuth:
    global _device_auth
    if _device_auth is None:
        _device_auth = auth_mod.DeviceAuth(store=get_control_store())
    return _device_auth



def get_control_store() -> storage_mod.ControlStore:
    global _control_store
    if _control_store is None:
        _control_store = storage_mod.ControlStore()
    return _control_store


def get_coordinator() -> coordinator_mod.CommandCoordinator:
    global _coordinator
    if _coordinator is None:
        executor = ha_executor_mod.HAExecutor(
            ha, queue_resolver=lambda pid: get_ma().queue_state(pid))
        _coordinator = coordinator_mod.CommandCoordinator(
            store=get_control_store(), executor=executor)
    return _coordinator


# 网关脚本名 → 核心动作（控制核心可路由的确定性动作）
SCRIPT_TO_ACTION: dict[str, str] = {
    "music_next": contracts.ACTION_NEXT,
    "music_previous": contracts.ACTION_PREVIOUS,
    "music_pause": contracts.ACTION_PAUSE,
    "music_resume": contracts.ACTION_RESUME,
    "music_stop": contracts.ACTION_STOP,
    "music_volume_set": contracts.ACTION_VOLUME_SET,
}


# ---- IMP-07：/api/v1 依赖装配 ----
class _MAReaderAdapter:
    """把 context.MAClient 适配成 playback_state 需要的读取接口。"""

    def __init__(self, mac) -> None:
        self.mac = mac

    def get_player(self, player_id: str):
        for p in (self.mac.api("players/all") or []):
            if p.get("player_id") == player_id:
                return p
        return None

    def get_active_queue(self, player_id: str):
        return self.mac.queue_state(player_id)

    def queue_items(self, queue_id: str, limit: int = 10):
        return self.mac.queue_items(queue_id, limit)


def get_snapshot() -> dict[str, Any]:
    reader = _MAReaderAdapter(get_ma())
    binding = devices_mod.get_target_player(get_control_store())
    return playback_state_mod.build_snapshot(reader, binding["player_id"])


api_v1_mod.init_deps(
    coordinator=get_coordinator,
    store=get_control_store,
    auth=get_device_auth(),
    device_auth_required=DEVICE_AUTH_REQUIRED,
    snapshot=get_snapshot,
    scenes=lambda: scene_catalog_mod.catalog_report(
        scene_catalog_mod.load_scenes(SCENES_FILE)),
    check_origin=lambda origin: auth_mod.check_origin(origin,
                                                      ALLOWED_ORIGINS),
    search=lambda q: get_ma().search(q, limit=6),
)
app.include_router(api_v1_mod.router)


_health_checker: health_mod.HealthChecker | None = None


def get_health_checker() -> health_mod.HealthChecker:
    global _health_checker
    if _health_checker is None:
        store = get_control_store()
        ma = get_ma()
        _health_checker = health_mod.HealthChecker(
            db_path=str(store.db_path),
            ha_base=HA_BASE, ha_token=ha._token(),
            ma_probe=lambda: ma.api("players/all") is not None,
            target_player_available=lambda: bool(
                (ma.pick_player() or {}).get("available", False)),
            stt_base=STT_BASE,
            nas_expected_source="",           # Docker 卷挂载无需匹配 NFS 来源
            nas_mount_point="/music",          # Docker 容器内路径
            control_db_probe=lambda: store.db_path.exists(),
        )
    return _health_checker


def _require_device(request_headers, body_device_id: str = "") -> str | None:
    """设备鉴权依赖（DEVICE_AUTH_REQUIRED=true 时强制）。

    返回 device_id（通过）或 None（拒绝，已写 reason）。
    """
    if not DEVICE_AUTH_REQUIRED:
        return body_device_id or "local"
    device_id = request_headers.get("X-Device-ID", "")
    token = request_headers.get("X-Device-Token", "")
    auth = get_device_auth()
    if not auth.authenticate(device_id, token):
        return None
    if not auth.check_rate(device_id):
        return None
    return device_id


def _check_origin(request_headers) -> bool:
    origin = request_headers.get("Origin") or request_headers.get("origin")
    return auth_mod.check_origin(origin, ALLOWED_ORIGINS)


def get_sessions() -> session_mod.SessionStore:
    global _sessions
    if _sessions is None:
        _sessions = session_mod.SessionStore(max_turns=CONVERSATION_TURNS)
    return _sessions


def get_ma() -> context_mod.MAClient:
    global _ma
    if _ma is None:
        _ma = context_mod.MAClient(env_loader=_env)
    return _ma


def get_history() -> context_mod.TrackHistory:
    global _history
    if _history is None:
        _history = context_mod.TrackHistory()
    return _history


# ------------------------------------------------------------------ 规则意图表（快路径）

# 顺序敏感：越具体的规则越靠前。
# ⚠️ 陷阱：「安静音乐」包含子串「静音」，会被 pause 规则误命中（Phase 5 实测踩过）。
#    所以所有控制类规则都改用锚点/边界，避免吃到描述性词汇。
RULES: list[tuple[str, str, str]] = [
    ("volume_up",    "music_volume_up",   r"(大声|大一点|大点|音量[^，。]{0,4}(大|加|调高)|响一点|太小声)"),
    ("volume_down",  "music_volume_down", r"(小声|小一点|小点|音量[^，。]{0,4}(小|减|调低)|太吵|太响)"),
    ("next",         "music_next",        r"(下一首|下个|换一首|跳过|切歌|下一曲|换一个)"),
    ("previous",     "music_previous",    r"(上一首|上个|退一首|上一曲|前一首)"),
    ("pause",        "music_pause",       r"(暂停|停一下|先停|别放)"),
    ("resume",       "music_resume",      r"(继续放|继续播|接着放|接着播|恢复播放|继续)"),
    ("play_pause",   "music_play_pause",  r"(^播放$|^开始播放$|^放歌$|播吧|放吧)"),
    ("stop",         "music_stop",        r"(停止播放|别放了|关掉音乐|停掉|停止|停下来|停下|不要放了)"),
    # ---- 上下文查询 ----
    ("context",      None,                r"(这首歌|当前.*(歌|曲|放)|现在放的?是什么|在放什么)"),
]

PLAY_PATTERNS = [
    r"(?:播放|给我放|放一下|放|播|来一点|来点|来首|来一首|听一下|听|我想听|我要听)(?:一点|点|一首|首|个)?(?P<q>[^\s，。,.]{1,20})",
]
STOPWORDS = {
    "音乐", "歌", "歌曲", "歌曲吧", "一点", "点", "什么", "这个", "那个", "吧", "啊", "了",
    "一下", "点音乐", "首歌", "首", "个",
}

# ---------------------------------------------------------------- 音量（绝对值/静音）
# ★ 2026-09-13 新增。实测缺口：说「把音量调到30」时**没有对应的绝对音量动作**，
#   规则与 LLM 都只能塞进 volume_down，结果只掉一格（音量 88 → 86），
#   用户体感就是「语音调音量没用」。同理「静音」当时被当成 pause。
#   所以这里先抓「音量 + 数字」，命中就走 volume_set。
VOLUME_WORD = r"(?:音量|声音|响声|音量大小|喇叭)"
_VOL_VERB = r"(?:调到|调成|调至|调整为|设为|设置成|设置到|开到|放到|打到|改成|变成|到|至|为|成|是)"
# ⚠️ 中文数字必须**单独**匹配（不能和「一点」的「一」混）：
#    「音量小一点」里的「一」如果被数字分支吃掉，就变成 volume_set(1) —— 静音了。
#    所以分成两支：
#      ① 音量词 + 可选动词 + **阿拉伯数字**（ASR 输出音量数字基本都是阿拉伯数字）
#      ② 音量词 + **必须带动词** + 中文数字（「音量调到三十」）
VOLUME_SET_RE = re.compile(
    VOLUME_WORD + r"\s*" + _VOL_VERB + r"?\s*(?:百分之\s*)?"
    r"(?P<num>[0-9]{1,3})\s*(?:%|％|percent|巴仙)?"
    r"|"
    + VOLUME_WORD + r"\s*" + _VOL_VERB + r"\s*(?:百分之\s*)?"
    r"(?P<num2>[零一二三四五六七八九十百两]{1,4})"
)
# 「百分之三十」这类：显式百分比，最不容易误判
VOLUME_PERCENT_RE = re.compile(
    r"百分之\s*(?P<num>[0-9]{1,3}|[零一二三四五六七八九十百两]{1,4})")
# 音量的口语量词（不带数字）
VOLUME_SPOKEN = (("一半", 50), ("最大", 100), ("最小", 0), ("全开", 100),
                 ("满格", 100), ("最小声", 0), ("最大声", 100))
# 静音：两层护栏
#   ① lookbehind：(?<![安平冷]) —— 挡住「安静/平静/冷静」
#   ② lookahead：(?![音乐]) —— 挡住「静音音乐」「静音音」这类把「静音」当形容词的
#      连写（「把音乐静音」仍然命中，因为静音在句尾）
# IMP-02（T15）：「闭麦」是输入设备控制，不再混入音乐静音正则。
MUTE_RE = re.compile(r"(?<![安平冷])(静音|别出声|闭嘴|消音|mute)(?![音乐])")
UNMUTE_RE = re.compile(
    r"(取消静音|解除静音|恢复音量|恢复声音|声音开回来|打开声音|开声|unmute)")
# 闭麦/麦克风开关：显式不支持（MIC_CONTROL_UNSUPPORTED），提示用设备关闭键。
MIC_RE = re.compile(r"(闭麦|取消闭麦|麦克风\s*(开|关|闭|静音))")

# 「取消静音」时恢复到多少（MA 的 mute_control 是 none，只能用音量 0 代替静音，
#  所以必须记住一个恢复值）。可用环境变量 UNMUTE_LEVEL 覆盖。
# IMP-02（T17）：优先恢复该播放器静音前的真实音量（执行静音时记录在
# _pre_mute_volume）；无已知值时用该保守默认，并如实告知「是默认值」。
UNMUTE_LEVEL = int(os.environ.get("UNMUTE_LEVEL", "40"))
_pre_mute_volume: dict[str, float] = {}


def resolve_unmute_level(stored) -> tuple[int, bool]:
    """取消静音的目标音量：有静音前记录 → 恢复它；否则保守默认。

    返回 (level, is_known_restore)。
    """
    if stored is not None and float(stored) > 0:
        return int(round(float(stored))), True
    return UNMUTE_LEVEL, False

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s: str) -> int | None:
    """把「三十」「二十」「十五」「一百」「35」解析成整数；失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            section = (section or 1) * 10
            total += section
            section = 0
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return None
    return total + section


# 静音/取消静音的执行语义（IMP-02，T17）：
#   music_mute   → 先记录该播放器当前音量（>0 时），再下调到 0；
#   music_unmute → 恢复记录中的静音前音量；无记录时用保守默认并如实说明。
_pre_mute_volume: dict[str, float] = {}


def _apply_mute_semantics(parsed: dict[str, Any]) -> None:
    """为 music_mute / music_unmute 计划解析最终 level（就地修改 variables）。

    MA 的 mute_control 为 none，静音以「音量 0」实现，因此必须记住静音前的
    音量。查不到播放器或音量时按「无已知值」处理，恢复使用保守默认。
    """
    try:
        player = get_ma().pick_player()
    except Exception:
        player = {}
    pid = (player or {}).get("player_id") or "default"
    parsed.setdefault("variables", {})
    if parsed["intent"] == "music_mute":
        vol = (player or {}).get("volume_level")
        if vol is not None and float(vol) > 0:
            _pre_mute_volume[pid] = float(vol)
        parsed["variables"]["level"] = 0
        return
    stored = _pre_mute_volume.pop(pid, None)
    level, known = resolve_unmute_level(stored)
    parsed["variables"]["level"] = level
    parsed["unmute_known"] = known
    if not known:
        parsed["say"] = ("已恢复到默认音量 %d（没找到静音前的音量记录）"
                         % UNMUTE_LEVEL)


def parse_volume_command(text: str) -> dict[str, Any] | None:
    """识别「绝对音量 / 静音 / 取消静音」。不是这类说法就返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    if MIC_RE.search(t):
        # IMP-02（T15）：闭麦是输入设备控制；网关无可信麦克风控制能力时
        # 显式返回 MIC_CONTROL_UNSUPPORTED，绝不能把音乐调成 0 冒充已闭麦。
        return {"intent": "mic_control_unsupported", "script": None,
                "variables": {},
                "say": "麦克风开关请使用设备上的关闭键，语音这边控制不了"}
    if UNMUTE_RE.search(t):
        # IMP-02（T17）：level 由执行段按「静音前音量」解析，无记录用保守默认。
        return {"intent": "music_unmute", "script": "music_volume_set",
                "variables": {}, "say": "音量已恢复"}
    if MUTE_RE.search(t):
        return {"intent": "music_mute", "script": "music_volume_set",
                "variables": {"level": 0}, "say": "已静音"}
    # 「音量开到一半」「声音最大」这类口语量词（必须有音量词，否则「最大」会误伤）
    if VOLUME_WORD and re.search(VOLUME_WORD, t):
        for word, lvl in VOLUME_SPOKEN:
            if word in t:
                return _volume_plan(lvl, None)
    m = VOLUME_PERCENT_RE.search(t) or VOLUME_SET_RE.search(t)
    if not m:
        return None
    raw = m.groupdict().get("num") or m.groupdict().get("num2")
    lvl = _cn_num(raw or "")
    if lvl is None or not (0 <= lvl <= 100):
        return None
    return _volume_plan(lvl, None)


def _volume_plan(level: int, say: str | None) -> dict[str, Any]:
    return {
        "intent": "volume_set",
        "script": "music_volume_set",
        "variables": {"level": int(level)},
        "say": say or ("已静音" if level == 0 else "音量已设为 %d" % level),
    }

# 这些表达规则判断不了 → 明确交给 LLM Agent
AGENT_TRIGGERS = (
    r"(换一种|换点|换个|来点|来一些|适合|场景|心情|氛围|风格|类似|有点像|不要有人声|没有人声|"
    r"安静|舒缓|轻快|治愈|放松|专注|写代码|写东西|看书|睡觉|助眠|跑步|开车|咖啡|下午|晚上|"
    r"太闹|太吵|听腻|无聊|随便|推荐|好听|冷门|经典|怀旧)"
)

# v0.3 §12.2：指代/上下文词。命中则**禁止走规则快路径**——
# 例如「这个别再放了」会被 pause 规则的「别放」误命中（真实隐患），
# 这些词必须交给带 Session 上下文的 Agent 消解。
CONTEXT_REF = re.compile(
    r"(这首|这个|这种|这类|刚才|之前那首|还是|再来|多点|少点|后面|后面快|"
    r"别再放|不喜欢这个|挺适合)"
)


# 规则路径的播报语（Phase 5 TTS 用；无需过 LLM，1ms 内返回）
# IMP-02：HTTP 受理 ≠ 播放器确认，停止/暂停改用受理措辞。
RULE_SAY: dict[str, str] = {
    "volume_up": "音量已调大",
    "volume_down": "音量已调小",
    "next": "下一首",
    "previous": "上一首",
    "pause": "好的，正在暂停播放",
    "resume": "继续播放",
    "play_pause": "好的",
    "stop": "好的，正在停止播放",
}


def parse_rules(text: str) -> dict[str, Any]:
    """快路径：纯规则解析。返回 intent/script/variables/say。"""
    t = (text or "").strip()
    if not t:
        return {"intent": "empty", "script": None, "variables": {}, "say": ""}

    # ★ 绝对音量 / 静音优先判：它们需要抓数字，放进通用 RULES 里会把
    #   「音量调到30」当成 volume_down（实测就是这样丢掉数字的）。
    vol = parse_volume_command(t)
    if vol is not None:
        return vol

    for intent, script, pat in RULES:
        if re.search(pat, t):
            return {"intent": intent, "script": script, "variables": {},
                    "say": RULE_SAY.get(intent, "")}

    for pat in PLAY_PATTERNS:
        m = re.search(pat, t)
        if m:
            q = _clean_query(m.group("q"))
            if q and len(q) >= 2:
                return {"intent": "play_query", "script": "music_play_query",
                        "variables": {"query": q, "media_type": "track"},
                        "say": "好的，为你播放%s" % q}

    return {"intent": "unknown", "script": None, "variables": {}, "say": ""}


# 句尾冗余后缀，剥掉后搜索命中率明显更高（"周杰伦的歌" → "周杰伦"）
_TRAILING_SUFFIXES = (
    "的歌", "的音乐", "歌曲", "的歌曲", "的歌儿", "的曲子", "的", "音乐", "歌", "曲子", "曲",
)


def _clean_query(raw: str) -> str:
    """清理搜索词：去首尾标点 + 剥掉句尾冗余后缀。"""
    q = (raw or "").strip("，。,.:：!！?？、 　")
    changed = True
    while changed and len(q) > 2:
        changed = False
        for suf in _TRAILING_SUFFIXES:
            if q.endswith(suf) and len(q) - len(suf) >= 2:
                q = q[: -len(suf)]
                changed = True
                break
    return q.strip("，。,.:：!！?？、 　")


def needs_agent(text: str, rule_result: dict[str, Any]) -> bool:
    """判断是否该交给 LLM。规则已命中就不必再花一次调用。

    例外：规则命中的 play_query 若搜索词本身是「抽象场景描述」（含 AGENT_TRIGGERS
    里的心情/场景词，如「适合写代码的安静」），改写质量明显不如 LLM，也交给 Agent。
    具体的歌手/歌名（「周杰伦」）则保留规则路径，省一次调用。
    """
    if not AGENT_ENABLED or not get_llm().enabled:
        return False

    if rule_result["intent"] == "unknown":
        return bool(re.search(AGENT_TRIGGERS, text or ""))

    if rule_result["intent"] == "play_query":
        q = (rule_result.get("variables") or {}).get("query", "")
        return bool(re.search(AGENT_TRIGGERS, q))

    return False


# ------------------------------------------------------------------ LLM Agent（慢路径）

AGENT_SYSTEM_PROMPT = (
    "你是一个家庭音乐助手的意图解析器。用户会用中文口语下达音乐指令。\n"
    "规则：\n"
    "1. 只调用一个最匹配的工具，不要输出解释文字。\n"
    "2. 用户提到歌手/歌名 → music_play，query 直接填原名。\n"
    "3. 用户描述抽象场景/心情/风格 → music_play，"
    "把描述转成 2-6 个具体的中文搜索关键词（例如「适合写代码的安静音乐」→「安静的钢琴曲」）。\n"
    "4. 暂停/继续/上下首/停止 → music_transport。\n"
    "5. 音量的两种说法要分清：只说「大声点/小声点/太吵了」→ "
    "music_transport(volume_up / volume_down)；**报了具体数字**"
    "（「音量调到30」「声音开到一半」「音量40」「静音」）→ "
    "music_transport(action=\"volume_set\", level=数字)，数字即目标音量 0-100"
    "（静音=0，「一半」≈50）。绝对音量绝不能用 volume_down 代替。\n"
    "6. 询问当前播放内容 → music_context。\n"
    "7. 若完全不相关（例如闲聊、问天气），不要调用任何工具。\n"
    "8. 用户说「闭麦/关麦克风/取消闭麦」是**输入设备控制**，不是音乐指令："
    "不要调用任何音乐工具（该能力未接入，由设备按键处理）。"
)


def run_agent(text: str) -> dict[str, Any]:
    """慢路径：GLM function calling。返回解析结果，失败则降级。"""
    t0 = time.time()
    llm = get_llm()
    try:
        res = llm.chat_with_tools(
            messages=[
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            tools=tools_mod.TOOL_SCHEMAS,
            temperature=0.1,
            max_tokens=200,
        )
    except llm_mod.LLMError as e:
        return {"intent": "agent_error", "script": None, "variables": {},
                "agent_error": str(e)[:200], "llm_seconds": round(time.time() - t0, 3)}

    calls = res["tool_calls"]
    base = {"llm_seconds": res["elapsed"], "llm_usage": res.get("usage") or {}}

    if not calls:
        return {"intent": "agent_noop", "script": None, "variables": {},
                "agent_reply": (res.get("content") or "")[:200], **base}

    call = calls[0]
    fn_name = call.get("function", {}).get("name", "")
    raw_args = call.get("function", {}).get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except (json.JSONDecodeError, TypeError):
        return {"intent": "agent_badargs", "script": None, "variables": {},
                "agent_raw": str(raw_args)[:200], **base}

    try:
        planned = tools_mod.plan(fn_name, args)
    except tools_mod.ToolError as e:
        return {"intent": "agent_toolerror", "script": None, "variables": {},
                "agent_error": str(e)[:200], **base}

    if planned["kind"] == "query":
        return {"intent": "context_agent", "script": None, "variables": {},
                "agent_tool": fn_name, "agent_args": args, "say": planned["say"], **base}

    return {"intent": "agent_" + fn_name, "script": planned["script"],
            "variables": planned["variables"], "agent_tool": fn_name,
            "agent_args": args, "say": planned["say"], **base}


# ------------------------------------------------------------------ API

class CommandIn(BaseModel):
    text: str
    dry_run: bool = False
    mode: str = "auto"          # auto | rules | agent
    want_say: bool = True       # 是否附带播报语
    speak: bool = False         # 是否顺带合成音频（返回 base64）
    request_id: str = ""        # 幂等键（客户端重试用同一 id）
    device_id: str = "local"


class TTSIn(BaseModel):
    text: str
    voice: str = TTS_VOICE


# -------------------------------------------------- IMP-04 鉴权与健康

@app.get("/livez")
def livez() -> dict[str, Any]:
    return get_health_checker().liveness()


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    """readiness：分组件上报；LLM/STT 故障不拉低基本播放控制的可用性。"""
    return get_health_checker().readiness()


@app.post("/auth/pairing-code")
def auth_pairing_code(body: dict[str, Any]):
    """管理员签发一次性配对码（需 X-Admin-Key，部署窗口配置 ADMIN_KEY）。"""
    auth = get_device_auth()
    res = auth.issue_pairing_code(
        admin_key=str(body.get("admin_key") or ""),
        admin_key_required=bool(ADMIN_KEY))
    if not res.get("ok"):
        return JSONResponse({"error": "PERMISSION_DENIED"}, status_code=403)
    return res


@app.post("/auth/pair")
def auth_pair(body: dict[str, Any]):
    auth = get_device_auth()
    res = auth.pair(str(body.get("code") or ""),
                    device_name=str(body.get("device_name") or ""),
                    kind=str(body.get("kind") or "pwa"))
    if res is None:
        return JSONResponse({"error": "INVALID_OR_EXPIRED_CODE"},
                            status_code=403)
    return res


@app.post("/auth/revoke")
def auth_revoke(body: dict[str, Any]):
    auth = get_device_auth()
    if not auth.authenticate(str(body.get("admin_device_id") or ""),
                             str(body.get("admin_token") or "")):
        return JSONResponse({"error": "PERMISSION_DENIED"}, status_code=403)
    ok = auth.revoke(str(body.get("device_id") or ""))
    return {"ok": ok}


@app.get("/scenes")
def scenes() -> dict[str, Any]:
    """IMP-06：场景就绪度报告。未就绪/停用的场景显式标 disabled。"""
    scene_list = scene_catalog_mod.load_scenes(SCENES_FILE)
    return {"scenes": scene_catalog_mod.catalog_report(scene_list)}


@app.get("/health")
def health() -> dict[str, Any]:
    llm = get_llm()
    return {
        "status": "ok",
        "phase": "4+5",
        "ha_base": HA_BASE,
        "target_player": ha.state("sensor.ma_target_player"),
        "now_playing": ha.state("sensor.ma_now_playing"),
        "agent_enabled": AGENT_ENABLED and llm.enabled,
        "agent_session_enabled": AGENT_SESSION_ENABLED,
        "llm_model": llm.model if llm.enabled else None,
        "tts_enabled": TTS_ENABLED,
        "tts_voice": TTS_VOICE,
    }


@app.get("/intents")
def intents() -> dict[str, Any]:
    return {"rules": [{"intent": i, "script": s, "pattern": p} for i, s, p in RULES],
            "play_patterns": PLAY_PATTERNS,
            "agent_triggers": AGENT_TRIGGERS}


@app.get("/tools")
def tool_list() -> dict[str, Any]:
    return {"tools": [t["function"]["name"] for t in tools_mod.TOOL_SCHEMAS],
            "schemas": tools_mod.TOOL_SCHEMAS}


@app.post("/command")
def command(body: CommandIn, request: Request) -> dict[str, Any]:
    t0 = time.time()
    text = (body.text or "").strip()
    if len(text) > 500:
        return JSONResponse({"error": "text too long (max 500)"}, status_code=413)
    if not _check_origin(request.headers):
        return JSONResponse({"error": "origin not allowed"}, status_code=403)
    device_id = _require_device(request.headers, body.device_id)
    if device_id is None:
        return JSONResponse({"error": "DEVICE_AUTH_REQUIRED"}, status_code=401)
    rule_res = parse_rules(text)
    parsed = dict(rule_res)
    path = "rules"

    if body.mode == "agent" or (body.mode == "auto" and needs_agent(text, rule_res)):
        agent_res = run_agent(text)
        if agent_res.get("script") or agent_res["intent"].startswith("context"):
            # Agent 成功解析出可执行动作 → 采用
            parsed = agent_res
        elif rule_res.get("script"):
            # Agent 失败/无动作，但规则有可用结果 → 降级用规则，保住可用性
            parsed = dict(rule_res)
            parsed["agent_fallback"] = agent_res.get("intent", "agent_failed")
        else:
            parsed = {**agent_res}
        path = "agent"

    result: dict[str, Any] = {"text": text, "path": path, **parsed,
                              "dry_run": body.dry_run}

    if parsed.get("intent") in ("music_mute", "music_unmute") and not body.dry_run:
        _apply_mute_semantics(parsed)
        result["unmute_known"] = parsed.get("unmute_known")
        if parsed.get("say"):
            result["say"] = parsed["say"]

    if parsed.get("script") and not body.dry_run:
        action = (SCRIPT_TO_ACTION.get(parsed["script"])
                  if CONTROL_CORE_V1 else None)
        if action:
            # IMP-03c：确定性动作进控制核心（幂等/代际/STOP 屏障）
            cres = get_coordinator().submit(contracts.CommandRequest(
                device_id=(body.device_id or "local"),
                request_id=(body.request_id or uuid.uuid4().hex),
                action=action, args=dict(parsed.get("variables") or {}),
                player_id="", source="voice"))
            if cres.get("status") == "error":
                result["ok"] = False
                result["error"] = cres.get("error")
                result["player_confirmed"] = False
            else:
                result["ok"] = True
                result["command_id"] = cres.get("command_id")
                result["player_confirmed"] = bool(
                    cres.get("player_confirmed", False))
        else:
            result.update(ha.call_script(parsed["script"],
                                         parsed.get("variables")))
            result["player_confirmed"] = False
    elif parsed.get("script"):
        result["ok"] = True

    # 上下文查询：把当前播放信息带回来，让上层能播报
    if parsed["intent"] in ("context", "context_agent"):
        np_state = ha.state("sensor.ma_now_playing")
        result["now_playing"] = np_state
        if not result.get("say"):
            if np_state and np_state not in ("idle", "unknown", "unavailable", ""):
                result["say"] = "现在播放的是 %s" % np_state
            elif np_state in ("unknown", "unavailable", ""):
                result["say"] = "暂时读不到播放状态"
            else:
                result["say"] = "现在没有在播放音乐"

    if not body.want_say:
        result.pop("say", None)

    if body.speak and TTS_ENABLED and result.get("say"):
        try:
            import base64
            import asyncio
            audio = asyncio.run(tts_mod.synthesize(result["say"], TTS_VOICE))
            result["audio_b64"] = base64.b64encode(audio).decode("ascii")
            result["audio_mime"] = "audio/mpeg"
        except Exception as e:  # noqa: BLE001
            result["tts_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])

    result["elapsed"] = round(time.time() - t0, 3)
    _log(result)
    return result


# ------------------------------------------------------------------ /agent（v0.3）

class AgentIn(BaseModel):
    text: str
    session_id: str = "auto"      # "auto" 取当前活跃会话，没有则新建
    user_id: str = "default"
    source: str = "voice"
    dry_run: bool = False
    want_say: bool = True         # reply 是否随行（客户端决定播不播）
    speak: bool = False           # 顺带合成音频（base64）
    request_id: str = ""          # 幂等键（空则每次生成新命令）
    device_id: str = "local"


def _agent_fallback_command(body: AgentIn) -> dict[str, Any]:
    """Agent 层**被显式关闭**时，/agent 整体降级为旧 /command 逻辑（§30 回滚）。"""
    cmd = command(CommandIn(text=body.text, dry_run=body.dry_run,
                            want_say=body.want_say, speak=body.speak))
    return {"status": "ok" if cmd.get("ok", True) else "error",
            "intent": cmd.get("intent"), "reply": cmd.get("say", ""),
            "actions": [cmd.get("script")] if cmd.get("script") else [],
            "session_id": None, "trace_id": None,
            "degraded": "agent_session_disabled", "path": cmd.get("path")}


class AgentTimeout(RuntimeError):
    """Planner 超过硬超时。"""


def _run_with_hard_timeout(fn, seconds: float) -> Any:
    """在守护线程里跑 fn，超过 seconds 抛 AgentTimeout。

    urllib 的 timeout 只是 socket 空闲超时，服务端"慢慢吐字节"时拦不住
    （实测 12s 的 socket timeout 下请求仍跑了 25s）。所以这里另加一道
    总时长硬超时。超时后守护线程会自行跑完退出，不影响主链路。
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise AgentTimeout("planner exceeded %.1fs" % seconds)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _degrade_to_rules(body: AgentIn, session_id: str, trace_id: str,
                      reason: str) -> dict[str, Any]:
    """Agent 不可用（超时/异常）时的**纯规则**降级路径（§30）。

    ⚠️ 这里刻意不再调 LLM：Agent 超时往往正是因为 LLM 在排队，
       此时若再发起一次 LLM 请求，延迟直接翻倍（实测 25s → 50s+）。
       规则能执行就执行；规则也搞不定的抽象描述则明确回话，不硬撑。
    """
    text = (body.text or "").strip()
    rule_res = parse_rules(text)
    result: dict[str, Any] = {"status": "ok", "intent": rule_res["intent"],
                              "reply": "", "actions": [],
                              "session_id": session_id, "trace_id": trace_id,
                              "degraded": reason}
    abstract_play = (rule_res["intent"] == "play_query"
                     and needs_agent(text, rule_res))
    if rule_res.get("script") and not abstract_play:
        if not body.dry_run:
            result.update(ha.call_script(rule_res["script"],
                                         rule_res.get("variables")))
        else:
            result["ok"] = True
        result["actions"] = [rule_res["script"]]
    elif abstract_play:
        # 「来点适合写东西的音乐」这类：规则只能拿到整句当搜索词，搜了也是白搜
        result["intent"] = "degraded_abstract"
        result["reply"] = "刚才没听清，再说一次好吗"
    if not body.want_say:
        result["reply"] = ""
    return result


def _reply_for_music_context(text: str, ctx: dict[str, Any]) -> str:
    """music_context 的答复：区分「这首」与「刚才那首」。"""
    np = ctx.get("now_playing") or {}
    if re.search(r"(刚才|之前)", text or ""):
        # recent_tracks 里最新一条通常就是当前曲目 → 按 uri（退化时按 title）
        # 排除它，取真正的前一首。
        for t in (ctx.get("recent_tracks") or []):
            if not t.get("title"):
                continue
            same = ((t.get("uri") and np.get("uri") and t["uri"] == np["uri"])
                    or (not t.get("uri") and t["title"] == np.get("title")))
            if not same:
                return "刚才那首是 %s%s" % (
                    t["title"], ("，%s 的" % t["artist"]) if t.get("artist") else "")
        return "这是本次播放的第一首，前面没有别的曲目了"
    if np.get("title"):
        return "现在播放的是 %s%s" % (
            np["title"], ("，%s 的" % np["artist"]) if np.get("artist") else "")
    return "现在没有在播放音乐"


def _local_hit(ma, query: str) -> bool:
    """本地库里有没有这首（判据：uri 以 library:// 开头）。

    ⚠️ 不能只看「搜索有没有结果」—— MA 的 music/search 是跨 provider 的，
       库里没有也会返回 QQ 音乐的在线条目（未登录时只能播约 60 秒试听）。
    """
    try:
        for t in (ma.search(query, limit=10) or []):
            if str(t.get("uri") or "").startswith("library://"):
                return True
    except Exception:
        return True      # 查不动就按「有」处理，避免误报「库里没有」
    return False


def _reply_for_play(ma, args: dict, query: str) -> str:
    """music_play 的播报语：本地有 → 正常播；本地没有 → 说明会补库。"""
    if args.get("allow_backfill") is False:
        return "好，给你放%s" % query
    if _local_hit(ma, query):
        return "好，给你放%s" % query
    return "库里没有%s，先放个在线版垫着，完整的我在后台下" % query


@app.post("/agent")
def agent_endpoint(body: AgentIn, request: Request) -> dict[str, Any]:
    """Session 化 Agent 入口（v0.3 §10）。

    流程：Fast Path（确定性命令直接执行）→ 取/建 Session → 构建 Music Context
          → Planner（GLM）→ 应用 session_update → 执行动作 → 回写 Session。
    """
    t0 = time.time()
    trace_id = uuid.uuid4().hex[:8]
    text = (body.text or "").strip()
    if len(text) > 500:
        return {"status": "error", "intent": "empty", "reply": "",
                "actions": [], "session_id": body.session_id,
                "trace_id": trace_id, "error": "text too long (max 500)"}
    if not _check_origin(request.headers):
        return {"status": "error", "error": "origin not allowed",
                "session_id": body.session_id, "trace_id": trace_id}
    device_id = _require_device(request.headers, body.device_id)
    if device_id is None:
        return {"status": "error", "error": "DEVICE_AUTH_REQUIRED",
                "session_id": body.session_id, "trace_id": trace_id}
    timings: dict[str, float] = {}

    if not text:
        return {"status": "error", "intent": "empty", "reply": "",
                "actions": [], "session_id": body.session_id,
                "trace_id": trace_id}
    if not AGENT_SESSION_ENABLED:
        return _agent_fallback_command(body)

    sessions = get_sessions()
    sess, is_new = sessions.get(body.session_id, body.user_id)
    sid = sess["session_id"]

    result: dict[str, Any] = {"status": "ok", "session_id": sid,
                              "trace_id": trace_id, "actions": []}

    # ---- Fast Path（§27）：确定性命令，但两类情况必须让位给 Agent ----
    #   ① 命中指代词（这个/刚才/还是/后面…）——规则无法消解，且会误命中
    #      （如「这个别再放了」会被 pause 规则的「别放」吃掉）
    #   ② 规则命中的 play_query 其实是抽象场景描述（「适合写东西的音乐」）——
    #      直接把整句当搜索词命中率极低，必须交给 LLM 改写。
    #      ⚠️ 2026-09-13 验收实测：漏了 ② 时 Case1 被降级成 play_query 走快路径。
    rule_res = parse_rules(text)
    if rule_res.get("intent") == "mic_control_unsupported":
        # IMP-02（T15）：闭麦不支持要如实说，不能静默，更不能动音乐音量。
        result.update({"intent": "mic_control_unsupported", "path": "fast",
                       "reply": rule_res["say"], "actions": []})
        sessions.append_turn(sess, text, "mic_control_unsupported",
                             rule_res["say"])
        timings["total"] = round(time.time() - t0, 3)
        result["timings"] = timings
        _log({"endpoint": "/agent", "trace_id": trace_id, "text": text, **result})
        return result
    if (rule_res.get("script") and rule_res["intent"] != "context"
            and not CONTEXT_REF.search(text)
            and not needs_agent(text, rule_res)):
        if rule_res.get("intent") in ("music_mute", "music_unmute")                 and not body.dry_run:
            _apply_mute_semantics(rule_res)
        action = (SCRIPT_TO_ACTION.get(rule_res["script"])
                  if CONTROL_CORE_V1 else None)
        if not body.dry_run:
            if action:
                cres = get_coordinator().submit(contracts.CommandRequest(
                    device_id=(body.device_id or "local"),
                    request_id=(body.request_id or uuid.uuid4().hex),
                    action=action, args=dict(rule_res.get("variables") or {}),
                    player_id="", source="voice"))
                if cres.get("status") == "error":
                    result["ok"] = False
                    result["error"] = cres.get("error")
                else:
                    result["ok"] = True
                    result["command_id"] = cres.get("command_id")
                result["player_confirmed"] = bool(
                    cres.get("player_confirmed", False))
            else:
                result.update(ha.call_script(rule_res["script"],
                                             rule_res.get("variables")))
                result["player_confirmed"] = False   # 受理 ≠ 播放器确认
        else:
            result["ok"] = True
        if rule_res.get("intent") == "music_unmute":
            # IMP-02（T17）：无静音前记录时必须告知「用的是默认音量」，
            # 不能让「音量已恢复」冒充真实恢复。
            result["unmute_known"] = rule_res.get("unmute_known", False)
            if not rule_res.get("unmute_known", True):
                result["reply"] = rule_res.get("say", "")
        # §9.1：动作本身就是回答，Fast Path 默认不给 TTS 文本。
        # 例外：点播的歌本地库没有 —— 这时会播在线试听并后台补库，不说明一句，
        #      用户只会觉得「怎么才一分钟就停了」。
        reply = ""
        if rule_res["intent"] == "play_query":
            q = (rule_res.get("variables") or {}).get("query") or ""
            if q and not _local_hit(get_ma(), q):
                reply = "库里没有%s，先放个在线版垫着，完整的我在后台下" % q
        result.update({"intent": rule_res["intent"], "path": "fast",
                       "reply": reply,
                       "actions": [rule_res["script"]]})
        sessions.append_turn(sess, text, rule_res["intent"], reply)
        timings["total"] = round(time.time() - t0, 3)
        result["timings"] = timings
        _log({"endpoint": "/agent", "trace_id": trace_id, "text": text,
              **{k: v for k, v in result.items() if k != "audio_b64"}})
        return result

    # ---- Smart Path（§28）：Context → Planner → 动作 ----
    ma, history = get_ma(), get_history()
    t_ctx = time.time()
    ctx = context_mod.build_context(ma, history, sess, max_tracks=CONTEXT_TRACKS)
    timings["context"] = round(time.time() - t_ctx, 3)

    t_llm = time.time()
    try:
        planned = _run_with_hard_timeout(
            lambda: agent_mod.run(text, ctx, get_llm()), AGENT_HARD_TIMEOUT)
    except AgentTimeout as exc:
        planned = {"intent": "agent_error", "action": None, "session_updates": [],
                   "error": str(exc)[:200], "llm_seconds": None, "llm_usage": {}}
    timings["agent"] = round(time.time() - t_llm, 3)

    if planned["intent"] == "agent_error":
        # Agent 层超时/异常 → 纯规则降级（§30）。刻意不再调 LLM，避免二次排队。
        fb = _degrade_to_rules(body, sid, trace_id,
                               planned.get("error", "agent_error"))
        fb["timings"] = timings
        sessions.append_turn(sess, text, fb.get("intent", "agent_error"),
                             fb.get("reply", ""))
        _log({"endpoint": "/agent", "trace_id": trace_id, "text": text, **fb})
        return fb

    # 应用 session_update（场景/约束回写）
    for upd in planned.get("session_updates") or []:
        sessions.update_scene(sess, scene=upd.get("scene", ""),
                              goal=upd.get("goal", ""),
                              duration_min=upd.get("duration_min", 0),
                              constraints=upd.get("constraints"))

    action = planned.get("action")
    reply = ""
    t_act = time.time()
    if action is None:
        result["intent"] = planned["intent"]
        # 把 Planner 的错误原因透出到响应里，否则 agent_toolerror 只能看到
        # 一个光秃秃的 intent，排障时完全不知道哪个工具、哪个参数不合法。
        if planned.get("error"):
            result["agent_error"] = planned["error"]
        if planned["intent"] == "agent_noop":
            # 与音乐无关或没听懂 → 静默（§9「动作就是回答」/ §18「少问废话」）。
            # ⚠️ 刻意不播报 reply_draft：实测推理模型会返回「与音乐无关，不调用
            #    任何工具」「（无工具调用）」这类**元说明**，播出来只会打扰用户。
            reply = ""
        else:
            # IMP-02：只更新了约束、没有任何播放动作时，不得暗示「后面已调整」
            # ——约束要等 IMP-10 的策略层才有真实作用。如实说明尚未生效。
            reply = planned.get("reply_draft") or "已记录，不过约束还没有应用到播放队列"
    else:
        plan = action["plan"]
        kind = plan["kind"]
        result["intent"] = planned["intent"]
        result["agent_tool"] = action["tool"]
        result["agent_args"] = action["args"]

        if kind == "script":
            if plan.get("script") == "music_volume_set" and                     planned.get("intent") in ("music_mute", "music_unmute"):
                _apply_mute_semantics(plan)
            if not body.dry_run:
                result.update(ha.call_script(plan["script"],
                                             plan.get("variables")))
                result["player_confirmed"] = False   # 受理 ≠ 播放器确认
            else:
                result["ok"] = True
            result["actions"].append(plan["script"])
            if action["tool"] == "music_play":
                reply = _reply_for_play(ma, action["args"],
                                        action["args"].get("query") or "")
            elif action["tool"] == "music_fetch":
                reply = "库里没有%s，我这就去找，下好自动播" % (
                    action["args"].get("query") or "")
            else:
                reply = ""  # transport 类不播报（§9.1）

        elif kind == "query":  # music_context
            reply = _reply_for_music_context(text, ctx)
            result["now_playing"] = ctx.get("now_playing")

        elif kind == "ma_search":
            tracks = ma.search(plan["variables"]["query"], limit=5)
            result["search_results"] = [
                {"title": t.get("name"),
                 "artist": " / ".join(a.get("name", "")
                                      for a in (t.get("artists") or [])),
                 "uri": t.get("uri"), "playable": t.get("is_playable")}
                for t in tracks[:5]
            ]
            if result["search_results"]:
                top = result["search_results"][:3]
                reply = "找到这些：" + "；".join(
                    "%s%s" % (t["title"], ("（%s）" % t["artist"])
                              if t["artist"] else "") for t in top)
            else:
                reply = "曲库里没找到相关的内容"

        elif kind == "feedback":
            sessions.add_feedback(
                sess, plan["variables"]["signal"],
                target=ctx.get("now_playing") or {},
                note=plan["variables"].get("note", ""))
            sig = plan["variables"]["signal"]
            reply = {"strong_positive": "记下了，后面多来点这种",
                     "track_positive": "记下了",
                     "scene_positive": "记下了，这个场景就按这种来",
                     "strong_negative": "好，以后不放这首了",
                     "track_negative": "好，以后不放这首了",
                     "artist_negative": "好，以后避开这个歌手"}.get(sig, "记下了")
    timings["action"] = round(time.time() - t_act, 3)

    if planned.get("session_updates"):
        result["session_updates"] = planned["session_updates"]
    if not reply and planned.get("session_updates") and not action:
        reply = "好，后面按这个来"

    result["reply"] = reply if body.want_say else ""
    if body.speak and TTS_ENABLED and reply:
        try:
            import base64
            import asyncio
            audio = asyncio.run(tts_mod.synthesize(reply, TTS_VOICE))
            result["audio_b64"] = base64.b64encode(audio).decode("ascii")
            result["audio_mime"] = "audio/mpeg"
        except Exception as e:  # noqa: BLE001
            result["tts_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])

    sessions.append_turn(sess, text, result.get("intent", "?"), reply)
    timings["total"] = round(time.time() - t0, 3)
    result["timings"] = timings
    _log({"endpoint": "/agent", "trace_id": trace_id, "text": text,
          "session_id": sid, "is_new_session": is_new,
          **{k: v for k, v in result.items() if k != "audio_b64"}})
    return result


@app.get("/agent/session")
def agent_session(user_id: str = "default") -> dict[str, Any]:
    """调试：查看当前 Session 仓库快照。"""
    return get_sessions().snapshot()


@app.post("/tts")
async def tts(body: TTSIn) -> Response:
    if not TTS_ENABLED:
        return JSONResponse({"error": "tts disabled"}, status_code=503)
    try:
        audio = await tts_mod.synthesize(body.text, body.voice)
    except tts_mod.TTSError as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/tts/voices")
async def tts_voices() -> dict[str, Any]:
    try:
        voices = await tts_mod.list_voices("zh-CN")
    except tts_mod.TTSError as e:
        return {"error": str(e)[:200], "voices": []}
    return {"voices": voices, "default": TTS_VOICE}


def _log(entry: dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        safe = {k: v for k, v in entry.items() if k != "audio_b64"}
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe, ensure_ascii=False) + "\n")
    except Exception:
        pass
