#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按需补库闭环：本地库查不到 -> musicdl 补 -> MA 重扫 -> （可选）经 HA 播放。

产出协议：
  - 人类可读日志一律写 **stderr**
  - 加 `--json` 时，最后在 **stdout** 输出一行 JSON：
      {"ok": true/false, "reason": "...", "query": "...",
       "track": {"name":..., "uri":..., "duration":...},
       "files": [...], "rejected": [...], "played": true/false}

设计要点：
  1. **先查本地库**，命中就直接用（不重复下载）。
  2. 本地没有才走 musicdl，且**先过滤再下载**：时长过短、翻唱/片段/伴奏
     标记、查询词未命中歌名/歌手的一律拒绝 —— 匿名音源最容易下到垃圾片段。
  3. 下载由 `musicdl_fetch.py fetch` 完成（复用它的相关度排序 + 改名 + 报告），
     本脚本只负责决策、验收和收尾（重扫 / 播放 / 拒收隔离）。
  4. 拒收的文件**不删除**，移到 /tmp/musicdl_reject/ 供人工复核。
  5. 播放动作**经 HA script.music_play_uri 下发**（D13：不直连 MA 做音乐动作），
     HA 不可用时只告警、不判失败。

用法：
  python3 auto_backfill.py "周杰伦 晴天"                 # 完整闭环（会下载）
  python3 auto_backfill.py "周杰伦 稻香" --dry-run        # 只看决策，不下载
  python3 auto_backfill.py "周杰伦 稻香" --play           # 下载后经 HA 播放
  python3 auto_backfill.py "周杰伦" --pick 3             # 一次补 3 首不同作品（丰富曲库）
  python3 auto_backfill.py "稻香" --out /mnt/music/补库    # 指定落盘目录
  python3 auto_backfill.py "稻香" --json --dry-run        # 机器可读（给服务用）

繁简归一：查询与库内曲名、在线候选都会先折叠成简体再比。
  （GDStudio 等音源返回「周杰倫」，不折叠会误判「库里没有」→ 白下一遍。）

注意：musicdl 许可证为 PolyForm Noncommercial 1.0.0，仅限非商业用途。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MA_BASE = os.environ.get("MA_URL", "http://127.0.0.1:8095").rstrip("/") + "/api"
MA_TOKEN = os.environ.get("MA_TOKEN") or os.environ.get("MA_LONG_TOKEN") or ""

HA_BASE = os.environ.get("HA_URL", "http://127.0.0.1:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN") or os.environ.get("HA_ACCESS_TOKEN") or ""
HA_REFRESH_TOKEN = os.environ.get("HA_REFRESH_TOKEN") or ""
# ⚠️ 2026-09-13 实测：refresh_token 续期时 `client_id` 必须与**当初签发它的那个**
#    完全一致，否则 HA 回 400 {"error":"invalid_request"}。
#    原来这里写的是 `HA_BASE + "/"`，而 .env 里 HA_URL=http://192.168.1.50:8123，
#    令牌却是在 http://127.0.0.1:8123/ 下签发的 → 续期永远失败 →
#    「补库 downloading 成功、played 却 =false」，完整版永远替换不上。
#    现在优先读 HA_CLIENT_ID（由 ha_mint_token.py 写好），再退回本机回环地址。
HA_CLIENT_ID = (os.environ.get("HA_CLIENT_ID")
                or "http://127.0.0.1:8123/")

MUSICDL_DIR = Path(os.environ.get("MUSICDL_DIR", "/home/ai/musicdl-sandbox")).expanduser()
# 容器里没有 venv，可用 MUSICDL_PY / MUSICDL_SCRIPT 覆盖到镜像内的解释器与脚本
MUSICDL_PY = Path(os.environ.get("MUSICDL_PY", str(MUSICDL_DIR / ".venv/bin/python")))
MUSICDL_SCRIPT = Path(os.environ.get("MUSICDL_SCRIPT", str(MUSICDL_DIR / "musicdl_fetch.py")))

FS_PROVIDER = os.environ.get("MA_FS_PROVIDER", "filesystem_local--SAMYr7be")
REJECT_DIR = Path(os.environ.get("REJECT_DIR", "/tmp/musicdl_reject"))

# 明显不是「原唱正片」的标记（与 musicdl_fetch.relevance 保持一致）
# ⚠️ 教训（2026-09-13）：漏了器乐改编类标记，导致「周杰伦 七里香」下回了
#    「樱晚haruka - 【八音盒】周杰伦-七里香」——歌名里带着原唱歌手和歌名，
#    但演唱者完全不是本人。器乐/改编类标记必须单独列全。
BAD_MARKS = (
    # 翻唱 / 改编
    "cover", "翻唱", "remix", "dj", "深情版", "女声版", "男声版", "童声",
    "清唱", "acapella", "ai翻唱", "ai cover", "ai 翻唱",
    # 器乐 / 改编音色（★ 上面那次就栽在这里）
    "八音盒", "音乐盒", "纯音乐", "器乐", "无人声", "off vocal",
    "钢琴", "吉他", "小提琴", "古筝", "二胡", "萨克斯", "电子琴", "口琴",
    "笛", "琵琶", "古琴", "竖琴", "instrumental", "变奏",
    # 片段 / 铃声 / 卡点
    "片段", "试听", "铃声", "高潮版", "卡点", "变速", "加速版", "慢版",
)

# ★ 加密 / DRM 封装格式 —— **硬拒收**（2026-09-13 实测新增）
#   教训：搜「周杰伦 七里香」时决策层放行了 `mgg`（酷狗加密 ogg，音质分 55），
#   下载只落了一个 .lrc、没有音频 → job 结果 `no_file_produced`
#   （日志原文：「七里香 - 周杰伦 只拿到歌词、没有音频」），白跑 3 分钟。
#   而且 Music Assistant 解不了这些格式，就算下下来也播不出声。
ENCRYPTED_EXTS = (
    # 酷狗
    "mgg", "mgg1", "mggl", "mflac", "mflac0", "kgm", "kgma", "kgg", "vpr",
    # QQ 音乐
    "qmc0", "qmc2", "qmc3", "qmcflac", "qmcogg", "tkm",
    # 网易云
    "ncm",
    # 虾米 / 咪咕
    "xm", "mg3",
)


def log(*a):
    print(*a, file=sys.stderr)


# ---------------- HTTP ----------------
def _req(url, data=None, headers=None, method=None, timeout=60, form=False):
    if form and data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdr = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        body = json.dumps(data).encode() if data is not None else None
        hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    r = urllib.request.Request(url, data=body, headers=hdr, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return raw.decode("utf-8", "replace")


def call(command, **args):
    return _req(MA_BASE, {"command": command, "args": args},
                headers={"Authorization": "Bearer " + MA_TOKEN})


def ha_call(path, payload=None, method="POST", retry=True):
    """调 HA REST。401 时用 refresh_token 续期后重试一次。

    ⚠️ 续期用的 client_id 必须与签发 refresh_token 时一致（见文件头 HA_CLIENT_ID 注释）。
    """
    global HA_TOKEN
    url = HA_BASE + path
    headers = {"Authorization": "Bearer " + HA_TOKEN} if HA_TOKEN else {}
    try:
        return _req(url, payload, headers=headers, method=method)
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry and HA_REFRESH_TOKEN:
            try:
                tok = _req(HA_BASE + "/auth/token", form=True, data={
                    "client_id": HA_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": HA_REFRESH_TOKEN,
                })
                HA_TOKEN = tok.get("access_token", "")
                headers = {"Authorization": "Bearer " + HA_TOKEN}
            except Exception as re_:
                return {"error": "ha_refresh_failed: %s" % re_}
            try:
                return _req(url, payload, headers=headers, method=method)
            except urllib.error.HTTPError as e2:
                # HA 400 多半是 script 不存在（如 music_play_uri 未部署）
                return {"error": "ha_http_%s_after_refresh: %s" % (e2.code, e2.reason)}
            except Exception as e2:
                return {"error": "ha_call_failed_after_refresh: %s" % e2}
        return {"error": "ha_http_%s: %s" % (e.code, e.reason)}
    except Exception as e:
        return {"error": "ha_call_failed: %s" % e}


def ha_play_uri(uri):
    """经 HA script.music_play_uri 播放（D13：音乐动作走 HA）。"""
    res = ha_call("/api/services/script/music_play_uri", {"uri": uri})
    if isinstance(res, dict) and res.get("error"):
        return False, res["error"]
    return True, ""


# ---------------- MA 查询 ----------------
def library_tracks():
    out, offset = [], 0
    while True:
        items = call("music/tracks/library_items", limit=200, offset=offset,
                     order_by="name") or []
        out.extend(items)
        if len(items) < 200:
            break
        offset += 200
    return out


def ma_sync(providers=None):
    try:
        res = call("music/sync", providers=providers or [FS_PROVIDER])
    except Exception as e:
        log("  [!] music/sync 失败：%s" % e)
        return None
    ids = []
    if isinstance(res, list):
        ids = [t.get("id") or t.get("task_id") for t in res if isinstance(t, dict)]
    for _ in range(30):
        time.sleep(2)
        done = True
        for tid in [i for i in ids if i]:
            try:
                t = call("tasks/get", task_id=tid)
                st = str((t or {}).get("status", "")).lower()
                if st not in ("success", "finished", "failed", "error", "cancelled"):
                    done = False
            except Exception:
                done = False
        if done:
            break
    time.sleep(2)
    return res


def pick_player():
    try:
        players = call("players/all") or []
    except Exception:
        return None
    avail = [p for p in players if p.get("available")]
    for p in avail:
        if p.get("provider") == "squeezelite":
            return p
    return avail[0] if avail else None


# ---------------- 本地库匹配 ----------------
# 繁简归一：华语曲库繁简混用（GDStudio 返回「周杰倫」、用户说「周杰伦」），
# 不折叠就会出现「库里明明有、却判成没有」→ 白下一遍。
# zhconv 为纯 Python 包；缺失时退化为内置高频字表（与 musicdl_fetch.py 同源）。
try:
    from zhconv import convert as _zh_convert

    def to_simp(s):
        try:
            return _zh_convert(str(s or ""), "zh-cn")
        except Exception:          # noqa: BLE001
            return str(s or "")
except ImportError:                # pragma: no cover - 兜底路径
    _T2S_PAIRS = (
        "倫伦陳陈張张學学劉刘華华鄧邓麗丽孫孙靜静傑杰蕭萧騰腾楊杨瑋玮羅罗蘇苏綠绿"
        "飛飞兒儿樂乐團团動动車车許许瀧泷謙谦榮荣綺绮貞贞靚靓穎颖傳传賢贤齊齐黃黄"
        "慶庆費费鳳凤駒驹譚谭詠咏國国艷艳嫻娴關关葉叶鄭郑鋒锋謝谢衛卫蘭兰側侧軒轩"
        "韻韵詩诗憶忆蓮莲潔洁儀仪嶽岳瑩莹亞亚輪轮澀涩會会緯纬盧卢廣广禮礼嚴严勢势"
        "綸纶棟栋樑梁鴻鸿吳吴藍蓝時时謹谨喬乔賈贾鈞钧愛爱戀恋傷伤淚泪夢梦緣缘願愿"
        "諾诺約约舊旧過过這这誰谁麼么們们來来後后從从為为對对錯错讓让覺觉見见說说"
        "讀读寫写聽听聲声話话語语詞词記记錄录開开閉闭門门問问間间電电風风雲云陽阳"
        "陰阴隊队燈灯點点熱热斷断續续無无與与個个樣样東东馬马鳥鸟魚鱼龍龙鷹鹰樹树"
        "紅红銀银銅铜鐵铁鋼钢絲丝線线網网結结織织縫缝紛纷純纯寧宁歲岁億亿兩两凱凯"
        "別别劍剑勁劲務务勝胜勞劳勵励匯汇區区協协單单賣卖嚮向園园圍围圖图圓圆聖圣"
        "場场壞坏壓压壘垒處处備备複复復复徵征應应懷怀懸悬懼惧戰战戶户執执掃扫掛挂"
        "揚扬換换損损搖摇撐撑撲扑撥拨擇择擔担擴扩攔拦擺摆攜携攝摄攤摊敗败敵敌數数"
        "暢畅曆历書书條条極极構构橋桥機机檢检權权歡欢歐欧歷历殘残殺杀殼壳氣气決决"
        "沒没沖冲淨净準准溝沟滅灭滿满漁渔濟济濤涛濫滥濕湿灑洒灣湾災灾烏乌煩烦營营"
        "燦灿燒烧爐炉爭争爺爷牆墙獨独獲获環环現现產产畫画異异當当療疗發发監监蓋盖"
        "盤盘眾众矯矫碼码確确禱祷萬万種种積积稱称穩稳窮穷競竞筆笔節节範范築筑簡简"
        "簽签籃篮類类紀纪級级細细終终組组絕绝統统經经綜综維维綱纲綴缀編编緩缓練练"
        "締缔縣县縮缩總总績绩纖纤罰罚罷罢羈羁義义習习聞闻聯联聰聪職职肅肃脅胁脈脉"
        "腦脑腳脚腸肠膚肤臉脸臨临舉举艙舱藝艺號号蟲虫補补裝装規规視视親亲觀观訂订"
        "計计訊讯討讨訓训講讲論论設设訪访訴诉診诊註注評评試试該该詳详認认誠诚誤误"
        "請请諸诸謀谋識识譜谱議议護护變变讚赞豐丰豬猪貝贝負负財财貢贡貧贫貨货販贩"
        "責责貴贵買买貸贷貼贴賀贺資资賓宾賞赏賠赔賤贱賦赋質质賬账賴赖賺赚購购賽赛"
        "贈赠贊赞贏赢趕赶趙赵趨趋跡迹踐践蹤踪軌轨軍军軟软較较載载輔辅輕轻輛辆輝辉"
        "輩辈輯辑輸输轄辖轉转轟轰辭辞辯辩農农迴回週周進进遊游運运達达違违遙遥遞递"
        "遠远適适遲迟遺遗遼辽邁迈還还邊边邏逻郵邮鄉乡鄰邻醜丑醫医釀酿釋释針针釘钉"
        "釣钓鈴铃銘铭銷销銳锐鋪铺錢钱錦锦錶表鍵键鍾钟鎖锁鎮镇鏈链鏡镜鐘钟鑄铸鑑鉴"
        "長长閒闲闡阐陣阵階阶隨随險险隱隐際际難难霧雾靈灵韓韩頁页頂顶項项順顺須须"
        "預预頑顽頓顿頌颂領领頭头頸颈頻频顆颗題题額额顏颜顧顾顫颤顯显飢饥飯饭飲饮"
        "飾饰飽饱餅饼養养餘余館馆饅馒馳驰駐驻駕驾騎骑驚惊鬆松鬥斗魯鲁鮮鲜鳴鸣鴨鸭"
        "鹹咸麥麦黨党齒齿龜龟"
    )
    _T2S = str.maketrans({_T2S_PAIRS[i]: _T2S_PAIRS[i + 1]
                          for i in range(0, len(_T2S_PAIRS) - 1, 2)})

    def to_simp(s):
        return str(s or "").translate(_T2S)


def norm(s):
    return re.sub(r"[\s　（）()【】\[\]·、,，.。!！?'\"’“”-]+", "",
                  to_simp(s)).lower()


# 版本后缀剥离（与 musicdl_fetch.strip_version 保持同源，改动要同步）
# 『稻香 (Live)』『稻香（治愈版）』→『稻香』；『晴天+稻香』不动（是另一个作品）。
_BRACKET_RE = re.compile(r"[（(\[【〔]([^）)\]】〕]{1,30})[）)\]】〕]")
_VER_KW = ("版", "live", "remix", "demo", "现场", "演唱会", "伴奏", "纯音乐",
           "instrumental", "cover", "翻唱", "重制", "重置", "修复", "串烧",
           "medley", "remaster", "acoustic", "不插电", "合唱版")


def strip_version(song):
    def _sub(m):
        inner = m.group(1).lower()
        return "" if any(k in inner for k in _VER_KW) else m.group(0)

    return _BRACKET_RE.sub(_sub, str(song or "")).strip()


def local_hit(query, tracks, threshold=55):
    q = norm(query)
    best, best_score = None, 0
    for t in tracks:
        name = norm(t.get("name"))
        if not name:
            continue
        score = 0
        if name == q:
            score = 100
        elif name and name in q:
            score = 80
        elif q and q in name:
            score = 70
        for tok in re.split(r"[\s　]+", str(query).strip()):
            tn = norm(tok)
            if len(tn) < 2 or tn not in name:
                continue
            # 覆盖率要求：只匹配到歌手（如「周杰伦」命中
            # 「周杰伦[男女情歌对唱冠军全记录]」）不算命中，否则会误判成「已有」。
            cov = len(tn) / max(len(name), 1)
            score = max(score, 55 if cov >= 0.5 else 40)
        if score > best_score:
            best, best_score = t, score
    return (best, best_score) if best_score >= threshold else (None, 0)


def track_by_song(tracks, song, singer=""):
    """按歌名（+歌手）在 MA 库里定位一首 track。

    用途：pick>1 时查询词可能是「周杰伦」（歌手），而刚下回来的具体歌名才是
    精确锚点 —— local_hit() 只比歌名，会漏掉。这里补一层歌名反查。
    """
    sn, gn = norm(strip_version(song)), norm(singer)
    if not sn:
        return None
    best, best_score = None, 0
    for t in tracks:
        name = norm(strip_version(t.get("name")))
        if not name:
            continue
        score = 0
        if name == sn:
            score = 100
        elif sn in name or name in sn:
            score = 70
        if not score:
            continue
        if gn:
            artists = " ".join(str((a or {}).get("name", ""))
                               for a in (t.get("artists") or []))
            if gn in norm(artists):
                score += 20
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= 70 else None


def in_library(tracks, song, singer=""):
    """库里是否**确实**已有这首歌。

    规则：把**候选**的版本后缀剥掉后，与库内曲名（原样归一，不剥版本）全等。
      * 库有《晴天》、候选《晴天 (Live)》 → 命中（有晴天就够了，不必再下 Live）
      * 库有《稻香 (Live)》、候选《稻香》   → **不命中**（Live 不是正片，该补原版）

    为什么不用 track_by_song 的模糊匹配：这里的用途是「决定要不要跳过下载」，
    假阳性（把没有的判成已有）比假阴性（多下一遍）糟糕得多 —— 宁可多下。
    """
    sn = norm(strip_version(song))
    gn = norm(singer)
    if not sn:
        return None
    for t in tracks:
        if norm(t.get("name")) != sn:
            continue
        if gn:
            artists = " ".join(str((a or {}).get("name", ""))
                               for a in (t.get("artists") or []))
            an = norm(artists)
            if an and gn not in an and an not in gn:
                continue
        return t
    return None


# ---------------- musicdl 在线搜索 ----------------
# 搜索子进程的总预算：合并搜索上限 240s，若触发「逐源降级」还要再花最多
# 3×150s，所以这里给到 900s，别把降级路径掐死。
SEARCH_CALL_TIMEOUT = int(os.environ.get("MUSICDL_SEARCH_CALL_TIMEOUT", "900"))

# 单文件体积上限（MB）。聚合音源的 flac 常见 100~200MB/首 —— 实测抓到过
# 208MB 的《屋顶》和 215MB 的《晴天 (Live)》。家里这套是语音点播，
# 为单曲吃 200MB 不划算（群晖再大也经不起一天补几十首）。0 = 不限。
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "80"))


def online_candidates(query, sources, size=10, timeout=None):
    """调 musicdl_fetch.py search --json，返回 (rc, stdout, stderr)。

    ★ 用 --json 而不是刮表格：给 search 加一列（如「相关度」）就会让正则列错位，
      曾经真的踩过。结构化输出对列宽变化免疫。
    """
    if not MUSICDL_PY.exists() or not MUSICDL_SCRIPT.exists():
        raise RuntimeError("找不到 musicdl 沙箱：%s" % MUSICDL_DIR)
    cmd = [str(MUSICDL_PY), str(MUSICDL_SCRIPT), "search", query,
           "--sources", *sources, "--size", str(size), "--top", str(size), "--json"]
    p = subprocess.run(cmd, cwd=str(MUSICDL_DIR), capture_output=True,
                       text=True, timeout=timeout or SEARCH_CALL_TIMEOUT)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _fmt_dur(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "0:00"
    return "%d:%02d" % (int(v // 60), int(v % 60))


def parse_candidates_json(text):
    """解析 `search --json` 的输出（取最后一行 JSON 数组）。"""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not (line.startswith("[") and line.endswith("]")):
            continue
        try:
            data = json.loads(line)
        except Exception:                        # noqa: BLE001
            continue
        if not isinstance(data, list):
            continue
        out = []
        for i, c in enumerate(data):
            if not isinstance(c, dict):
                continue
            try:
                mb = round(float(c.get("file_size_bytes") or 0) / 1048576, 1)
            except (TypeError, ValueError):
                mb = 0.0
            out.append({
                "idx": i,
                "source": str(c.get("source") or ""),
                "song_name": str(c.get("song_name") or ""),
                "singers": str(c.get("singers") or ""),
                "dur": _fmt_dur(c.get("duration_s")),
                "size_mb": mb,
                "ext": str(c.get("ext") or ""),
                "relevance": c.get("relevance"),
            })
        return out
    return []


def parse_candidates(text):
    """兼容旧版：刮 search 的表格输出（仅在没有 --json 时兜底）。"""
    cands = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(.+?)\s{2,}(\S.*?)\s{2,}(\d+:\d\d)\s+([\d.]+)\s+(\S+)\s*$",
                     line)
        if not m:
            continue
        cands.append({
            "idx": int(m.group(1)), "source": m.group(2),
            "song_name": m.group(3).strip(), "singers": m.group(4).strip(),
            "dur": m.group(5), "size_mb": float(m.group(6)), "ext": m.group(7),
        })
    return cands


def dur_sec(s):
    try:
        mm, ss = str(s).split(":")
        return int(mm) * 60 + int(ss)
    except Exception:
        return 0


def reject_reason(c, query, min_duration):
    d = dur_sec(c["dur"])
    if d < min_duration:
        return "时长仅 %s（< %ds），疑似片段/试听" % (c["dur"], min_duration)
    # ★ 加密/DRM 格式一票否决：下了也只有歌词、MA 也解不了（见 ENCRYPTED_EXTS）
    _ext = str(c.get("ext") or "").strip().lower().lstrip(".")
    if _ext in ENCRYPTED_EXTS:
        return ("加密格式 .%s（酷狗/QQ/网易的 DRM 封装）—— 下载常只落歌词、"
                "且 MA 无法解码" % _ext)
    # 单曲超过 15 分钟基本是「整张专辑/合集」被当成一首上传。
    # 实测：搜「周杰伦 稻香」时冒出「稻香 (原唱 周杰伦) - 纪钧瀚」，标称 99:00。
    if d > 900:
        return "时长 %s 超过 15 分钟，疑似整张专辑/合集被当成单曲上传" % c["dur"]
    if MAX_FILE_MB and c.get("size_mb", 0) > MAX_FILE_MB:
        return "体积 %.0fMB 超过上限 %dMB" % (c.get("size_mb", 0), MAX_FILE_MB)
    low = (to_simp(c["song_name"]) + " " + to_simp(c["singers"])).lower()
    q_low = to_simp(query).lower()
    for mark in BAD_MARKS:
        # 标记词本身出现在**查询**里就不算垃圾 —— 否则《吉他手》（陈绮贞）、
        # 《钢琴师》这类合法歌名会被自己的标题误杀。
        if mark in low and mark not in q_low:
            return "命中垃圾标记「%s」" % mark
    # 串烧：歌名用 + / ＆ 拼了两首（如「晴天+稻香」），点单曲时不该给串烧
    name_raw = str(c["song_name"])
    if any(ch in name_raw for ch in "+＋＆") and not any(ch in query for ch in "+＋＆"):
        return "疑似串烧（歌名含「%s」）" % next(
            ch for ch in "+＋＆" if ch in name_raw)
    # ⚠️ 连接词也要切（2026-09-13 实测）：语音/网关常给「陈奕迅的十年」这种
    #   无空格查询，「的」把歌手+歌名粘成一个 token，永远匹配不上元数据，
    #   正经目标被误杀成「疑似非目标版本」。把 的/了/吧 等口语连接词一并切开。
    #   副作用只是「放宽」——每段子串各自需要命中歌名或歌手，不会放进垃圾。
    toks = [t for t in re.split(r"[\s　的了吧呢吗啊]+", str(query).strip()) if t]
    name_n, singer_n = norm(c["song_name"]), norm(c["singers"])
    for t in toks:
        tn = norm(t)
        if len(tn) < 2:
            continue
        if tn not in name_n and tn not in singer_n:
            return "查询词「%s」未出现在歌名/歌手里，疑似非目标版本" % t

    # ★ 器乐/改编兜底：查询里的词**全都在歌名里、一个都不在演唱者里**。
    #   典型如「周杰伦 七里香」命中「【八音盒】周杰伦-七里香」（歌手 樱晚haruka）。
    #   只认「歌名含 ≥2 个查询词且歌手零命中」这一种强特征，避免误杀单曲名查询。
    if len(toks) >= 2:
        in_name = [t for t in toks if len(norm(t)) >= 2 and norm(t) in name_n]
        in_singer = [t for t in toks if len(norm(t)) >= 2 and norm(t) in singer_n]
        if len(in_name) >= 2 and not in_singer:
            return ("歌名同时含 %d 个查询词但演唱者「%s」一个都不匹配 —— "
                    "高度疑似器乐改编/翻唱" % (len(in_name), c["singers"]))
    return None


# ---------------- 下载 ----------------
def do_fetch(query, sources, outdir, report_path, timeout=1500, pick=1,
             min_duration=0, approved=None):
    """调 musicdl_fetch.py fetch。pick>1 时下多条不同作品（丰富曲库）。

    ★ approved：把决策层筛好的清单**传给下载层**。不给的话 cmd_fetch 会自己
      重新搜、重新选 —— 那会绕过本脚本的所有守卫（含逐候选查库去重），
      实测导致库里已有的《晴天》被重复下载。
    """
    cmd = [str(MUSICDL_PY), str(MUSICDL_SCRIPT), "fetch", query,
           "--sources", *sources, "--out", str(outdir),
           "--report", str(report_path), "--pick", str(max(1, int(pick)))]
    if approved:
        ap = Path(str(report_path) + ".approved.json")
        ap.write_text(json.dumps(list(approved), ensure_ascii=False), encoding="utf-8")
        cmd += ["--approved", str(ap)]
    if min_duration:
        # 让脚本自己拦掉太短的文件（比事后整批拒收更精确：好的一首不会被差的拖累）
        cmd += ["--min-duration", str(int(min_duration))]
    p = subprocess.run(cmd, cwd=str(MUSICDL_DIR), capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def read_report(report_path):
    """解析 musicdl_fetch 的报告，返回 (moved_to, rejected_short, durations)。

    兼容两种格式：
      * 新版 dict：{"moved_to": [...], "rejected_short": [...], "files": [{duration_s}]}
      * 旧版 list：[{file,duration_s,path}, ..., {"moved_to": [...]}]
    """
    moved_to, short, durations = [], [], []
    p = Path(report_path)
    if not p.exists():
        return moved_to, short, durations
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                       # noqa: BLE001
        log("  [!] 报告解析失败：%s" % e)
        return moved_to, short, durations

    if isinstance(data, dict):
        moved_to = [str(x) for x in (data.get("moved_to") or [])]
        short = [str(x) for x in (data.get("rejected_short") or [])]
        for f in (data.get("files") or []):
            if isinstance(f, dict) and f.get("duration_s"):
                durations.append(float(f["duration_s"]))
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            if "moved_to" in item:
                moved_to = [str(x) for x in (item.get("moved_to") or [])]
            elif item.get("duration_s"):
                durations.append(float(item["duration_s"]))
    return moved_to, short, durations


def quarantine(paths):
    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in paths:
        src = Path(p)
        if not src.exists():
            continue
        dst = REJECT_DIR / ("%d_%s" % (int(time.time()), src.name))
        shutil.move(str(src), str(dst))
        moved.append(str(dst))
    return moved


# ---------------- 主流程 ----------------
def run(args):
    # pick 要早点定下来：>1 表示「充实曲库」意图，而不是「播放这一首」，
    # 两者在「本地命中要不要短路」上行为不同（见下）。
    pick = max(1, int(getattr(args, "pick", 1) or 1))
    result = {"ok": False, "reason": "", "query": args.query,
              "track": None, "tracks": [], "files": [], "rejected": [], "played": False,
              "local_hit": False, "pick": pick, "dry_run": bool(args.dry_run)}

    if not MA_TOKEN:
        log("[!] 没有 MA_TOKEN / MA_LONG_TOKEN —— 无法查库，直接退出。")
        log("    本地跑时先加载凭据：set -a && . /opt/home-music-agent/.env && set +a")
        result["reason"] = "no_ma_token"
        return result

    log("=== ① 查本地库：%s ===" % args.query)
    try:
        tracks = library_tracks()
    except Exception as e:
        result["reason"] = "ma_unreachable: %s" % e
        return result

    hit, score = local_hit(args.query, tracks)
    # ★ pick>1 = 补库意图，**不能**因为库里碰巧有同歌手的歌就短路：
    #   实测查「周杰伦」会命中库里的「周杰伦[男女情歌对唱冠军全记录]」(匹配分 70)，
    #   于是直接返回 local_hit、一首都不补。补库要的是「再找 N 首不同的歌」，
    #   该不该跳过由后面的**逐候选**查库（in_library）决定。
    if hit and pick <= 1:
        log("  本地已存在：%s（%ss，uri=%s，匹配分=%d）" %
            (hit.get("name"), hit.get("duration"), hit.get("uri"), score))
        result.update(ok=True, reason="local_hit", local_hit=True, track={
            "name": hit.get("name"), "uri": hit.get("uri"),
            "duration": hit.get("duration")})
        # IMP-02：补库完成只报告资源就绪（asset_ready），不再自动播放。
        result["asset_ready"] = True
        if args.play and not args.dry_run:
            result["autoplay_blocked"] = True
            log("  [IMP-02] 请求携带 --play：自动播放已禁用，仅入库不改变当前播放")
        elif args.dry_run:
            log("  [dry-run] 不触发播放")
        return result

    if hit:
        log("  本地已有《%s》（匹配分 %d），但 pick=%d 是补库意图 → 继续在线找更多作品"
            % (hit.get("name"), score, pick))
    else:
        log("  本地无命中（库内 %d 首），走在线补库" % len(tracks))

    log("")
    log("=== ② 在线搜索（%s）===" % ",".join(args.sources))
    try:
        rc, out, err = online_candidates(args.query, args.sources)
    except subprocess.TimeoutExpired:
        result["reason"] = "search_timeout"
        return result
    cands = parse_candidates_json(out) or parse_candidates(out)
    if not cands:
        tail = [l for l in (err or out).strip().splitlines() if l.strip()][-8:]
        log("  在线也没有结果。原始输出尾部：")
        log("  " + "\n  ".join(tail))
        result["reason"] = "no_online_candidates"
        result["raw_tail"] = tail
        return result

    log("  候选 %d 条，逐个过滤：\n" % len(cands))
    accepted = []
    already_n = other_n = 0
    for c in cands:
        why = reject_reason(c, args.query, args.min_duration)
        if not why:
            # ★ 逐候选查库：pick>1 时查询可能是「周杰伦」这种宽口径，
            #   候选里会带上库里早就有的歌（如已收藏的《晴天》）→ 重复下载。
            #   步骤① 的 local_hit 只比对整个查询词，管不到这里。
            t = in_library(tracks, c["song_name"], c["singers"])
            if t:
                why = "库里已有《%s》" % t.get("name")
                already_n += 1
        if why and not why.startswith("库里已有"):
            other_n += 1
        log("  [%s] %-34s %-18s %6s %7.1fMB %s%s" %
            ("拒" if why else "收", c["song_name"][:34], c["singers"][:18],
             c["dur"], c["size_mb"], c["ext"], ("  <- " + why) if why else ""))
        if not why:
            accepted.append(c)

    if not accepted:
        if already_n and not other_n:
            log("\n[结果] 候选全部已在库内 —— 无需补库。")
            result.update(ok=True, reason="all_in_library", local_hit=True)
            return result
        log("\n[结果] 没有一条能过过滤 —— 不下载。"
            "匿名音源常见这样，建议：换更精确的查询词，或增加音源。")
        result["reason"] = "all_candidates_rejected"
        result["candidates"] = cands
        return result

    best = accepted[0]
    extra = ("，另按 --pick %d 一并尝试同批候选" % pick) if pick > 1 else ""
    log("\n  首选：%s - %s（%s, %s, %.1fMB）%s" %
        (best["song_name"], best["singers"], best["dur"], best["ext"],
         best["size_mb"], extra))
    result["candidate"] = best
    result["candidates_accepted"] = accepted[:pick]

    if args.dry_run:
        log("\n[dry-run] 到此为止，未下载。")
        result.update(ok=True, reason="dry_run")
        return result

    log("")
    log("=== ③ 下载并入库 ===")
    outdir = Path(args.out)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        result["reason"] = "outdir_error: %s" % e
        return result
    report = Path("/tmp/backfill_report_%d.json" % int(time.time()))
    try:
        rc, out = do_fetch(args.query, args.sources, outdir, report, args.timeout,
                           pick=pick, min_duration=args.min_duration,
                           approved=result.get("candidates_accepted"))
    except subprocess.TimeoutExpired:
        result["reason"] = "fetch_timeout"
        return result
    log(out.strip()[-1500:])

    moved_to, short_files, durations = read_report(report)

    result["files"] = moved_to
    if short_files:
        q = quarantine(short_files)
        log("\n[拒收] %d 个文件时长不足 %ds，已隔离到 %s（未删除）"
            % (len(short_files), args.min_duration, REJECT_DIR))
        result["rejected"] = q

    if not moved_to:
        log("\n[!] 下载未产出可用文件")
        result["reason"] = "duration_too_short" if short_files else "no_file_produced"
        if durations:
            result["duration_s"] = max(durations)
        return result

    log("")
    log("=== ④ MA 重扫 ===")
    # ★ 用 uri 而不是 id：MA 的 track 对象没有 `id` 字段，集合会退化成 {None}，
    #   日志里因此出现过「1 -> 17」这种假数字（排查时被误导过一次）。
    before = {t.get("uri") for t in library_tracks()}
    ma_sync()
    after = library_tracks()
    log("  库内曲目数：%d -> %d" % (len(before), len(after)))

    # 定位本次入库的曲目
    found, seen_uri = [], set()

    def _add(t):
        u = t and t.get("uri")
        if u and u not in seen_uri:
            seen_uri.add(u)
            found.append(t)

    # pick>1 时**不要**用 local_hit：查询是宽口径（如「周杰伦」），它会命中库里的
    # 专辑名「周杰伦[男女情歌对唱冠军全记录]」，而不是刚下回来的《枫》——
    # 那样播放回调就会放错歌。宽口径一律按「已筛清单」逐首反查。
    if pick <= 1:
        hit, score = local_hit(args.query, after)
        if hit:
            _add(hit)
    for c in (result.get("candidates_accepted") or [])[:pick]:
        _add(track_by_song(after, c.get("song_name"), c.get("singers")))
    if not found:
        # 最后兜底：按刚入库的文件名反查
        for p in moved_to:
            _add(track_by_song(after, Path(p).stem))

    if not found:
        result.update(ok=True, reason="downloaded_but_not_indexed", files=moved_to)
        return result

    primary = found[0]
    log("  已入库 %d 首，主曲目：%s（%ss, %s）" %
        (len(found), primary.get("name"), primary.get("duration"), primary.get("uri")))
    result.update(ok=True, reason="downloaded", track={
        "name": primary.get("name"), "uri": primary.get("uri"),
        "duration": primary.get("duration")})
    result["tracks"] = [{"name": t.get("name"), "uri": t.get("uri"),
                         "duration": t.get("duration")} for t in found]

    # IMP-02：补库完成只入库（asset_ready），不改变任何音箱的播放。
    result["asset_ready"] = True
    if args.play:
        result["autoplay_blocked"] = True
        log("  [IMP-02] 请求携带 --play：自动播放已禁用，仅入库不改变当前播放")
    return result


def main():
    ap = argparse.ArgumentParser(description="按需补库闭环（本地优先，在线兜底）")
    ap.add_argument("query", help='如 "周杰伦 晴天"')
    ap.add_argument("--sources", nargs="+",
                    default=[s for s in os.environ.get(
                        "SOURCES",
                        "JBSouMusicClient,MituMusicClient,NeteaseMusicClient").split(",") if s],
                    help="musicdl 音源。默认取环境变量 SOURCES，"
                         "否则 JBSou+Mitu+网易云（实测原唱命中率最高的组合）")
    ap.add_argument("--out", default=os.environ.get("MUSIC_DIR", "/mnt/music"),
                    help="落盘目录（默认群晖音乐库根）")
    ap.add_argument("--min-duration", type=int, default=60, help="最短可接受时长（秒）")
    ap.add_argument("--pick", type=int, default=int(os.environ.get("BACKFILL_PICK", "1")),
                    help="下载几条**不同作品**。1 = 只补点播那一首（本地已有会短路）；"
                         ">1 = 充实曲库（跳过「本地已有同歌手歌」的短路，逐候选去重后补 N 首）")
    ap.add_argument("--play", action="store_true", help="成功后经 HA 播放")
    ap.add_argument("--dry-run", action="store_true", help="只做决策，不下载")
    ap.add_argument("--timeout", type=int, default=1500,
                    help="下载子进程超时（秒）。含搜索 240s + 逐源降级 3×150s + 下载，"
                         "所以给到 1500")
    ap.add_argument("--json", action="store_true", help="stdout 输出机器可读 JSON")
    args = ap.parse_args()

    result = run(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
