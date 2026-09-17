#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
musicdl 落地脚本：搜索 + 下载 + 校验 + 入库报告

用途：为家庭音乐系统补充本地音乐库（MA filesystem_local provider）

默认输出目录 = 群晖音乐库 /mnt/music（NFS 挂载点，需以 rw 挂载）。
MA 容器把 /mnt/music 挂为 /music，下载后 MA 会自动入库（每 12 小时增量同步，
或用 music/sync 立即触发）。

用法：
  # 搜索（不下载）
  python3 musicdl_fetch.py search "周杰伦 晴天"

  # 下载到群晖音乐库（默认 /mnt/music，可省略 --out）
  python3 musicdl_fetch.py fetch "周杰伦 晴天"

  # 指定子目录（如按类型分目录）
  python3 musicdl_fetch.py fetch "周杰伦 晴天" --out /mnt/music/无损

  # 限制格式以节省空间
  python3 musicdl_fetch.py fetch "周杰伦 晴天" --max-ext mp3

  # 指定音源
  python3 musicdl_fetch.py fetch "周杰伦 晴天" --sources QQMusicClient

  # 一次补多条不同作品（丰富曲库）
  python3 musicdl_fetch.py fetch "周杰伦" --pick 3

  # 批量（每行一个查询）
  python3 musicdl_fetch.py batch queries.txt

音源选择（2026-09-13 实测「周杰伦 稻香」原唱命中率）：
  JBSouMusicClient 10/10 ★ / MituMusicClient 5/6 / NeteaseMusicClient 1/10
  GDStudioMusicClient 有原唱但返回繁体，已由繁简归一修掉。

注意：
  musicdl 许可证为 PolyForm Noncommercial 1.0.0 —— 仅限非商业用途。
  请遵守所在地区版权法规，仅下载你有权获取的内容。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---- IPv4 优先 + 兜底超时 ----------------------------------------------
# VM1 存在「IPv6 黑洞」：部分域名 DNS 同时返回 A 和 AAAA，而 IPv6 出网不通，
# 表现为 TCP connect 永久挂起（music.163.com 实测 curl -6 超时、-4 仅 0.1s）。
# musicdl 内部的 requests 调用大多没传 timeout，一旦命中就整个 search 卡死。
# 这里在导入 musicdl 之前把 getaddrinfo 换成 IPv4 优先，并给 socket 兜底超时。
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    infos = _orig_getaddrinfo(host, port, family, type, proto, flags)
    if family in (0, _socket.AF_UNSPEC) and len(infos) > 1:
        infos = sorted(infos, key=lambda i: 0 if i[0] == _socket.AF_INET else 1)
    return infos


_socket.getaddrinfo = _ipv4_first
_socket.setdefaulttimeout(30)   # 兜底：任何没设 timeout 的请求最多等 30s

# musicdl 内部大量 requests 调用不传 timeout，遇到「接了连接不回包」的第三方
# 解析接口会永久挂起（实测 QQ 的 obtainqimei、部分网盘解析接口）。统一注入默认超时。
import requests as _requests

_req_orig = _requests.sessions.Session.request


def _request_with_timeout(self, method, url, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = (10, 30)   # (连接超时, 读取超时)
    return _req_orig(self, method, url, **kwargs)


_requests.sessions.Session.request = _request_with_timeout

# 搜索整体硬超时：即使某个音源卡住，也不让命令行无限等。
# 实测：三源（JBSou+Mitu+网易云）合并搜索稳定在 ~74s；但上游接口偶发抖动，
# 曾在容器里一次跑到 >120s 直接判超时、整个作业白跑。
# 所以默认给到 240s（≈3 倍余量），并留环境变量可调。
SEARCH_TIMEOUT_S = int(os.environ.get("MUSICDL_SEARCH_TIMEOUT", "240"))
# 逐源降级时的单源上限（JBSou 单独一次约 101s）
SOURCE_TIMEOUT_S = int(os.environ.get("MUSICDL_SOURCE_TIMEOUT", "150"))

# ---- musicdl 导入（兼容其内部真实路径）----
try:
    from musicdl.musicdl import MusicClient  # 真实类名（非 MusicDL）
    from musicdl.modules.sources.base import BaseMusicClient  # noqa: F401
except ImportError as e:
    print(f"[错误] 无法导入 musicdl: {e}")
    print("       请在 musicdl 的虚拟环境中运行：")
    print("       cd ~/musicdl-sandbox && .venv/bin/python musicdl_fetch.py ...")
    sys.exit(1)

# 默认音源（2026-09-13 实测「周杰伦 稻香」原唱命中率）：
#   JBSouMusicClient    10/10 命中 ★ 最优，但单次约 100s
#   MituMusicClient      5/6  命中，约 17s
#   NeteaseMusicClient   1/10 命中，约 50s（老牌但匿名态质量差）
#   GDStudioMusicClient  0/10 —— 其实有原唱，只是返回**繁体**「周杰倫」被漏判，
#                        已由 to_simp() 修掉，留作备选
# 说明：musicdl 会把每个音源都查一遍，音源越多越慢，按需增减。
DEFAULT_SOURCES = ["JBSouMusicClient", "MituMusicClient", "NeteaseMusicClient"]

# 默认输出目录 = 群晖音乐库（NFS rw 挂载点，MA 挂为 /music）
DEFAULT_OUT = "/mnt/music"

# 音质优先级（数值越大越好）
QUALITY_RANK = {
    "flac": 100, "ape": 95, "wav": 90, "320k": 80, "256k": 70,
    "192k": 60, "128k": 50, "m4a": 40, "aac": 35, "mp3": 30,
}

AUDIO_EXTS = (".flac", ".ape", ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".wma")

# ---- 繁简归一 -----------------------------------------------------------
# 华语曲库繁简混用是常态：GDStudio 等音源返回「周杰倫」，用户说的是「周杰伦」，
# 直接子串匹配会**全部落空**（实测「周杰伦 稻香」在 GDStudio 命中数被低估成 0）。
# zhconv 是纯 Python 包（无外部数据文件），不可用时退化为内置高频字表。
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


def norm_cmp(s):
    """比较用归一化：繁->简 + 去掉空白/标点 + 小写。"""
    return re.sub(r"[\s　（）()【】\[\]·、,，.。!！?'\"’“”-]+", "",
                  to_simp(s)).lower()


def _as_dict(obj):
    """把 SongInfo 对象转成 dict。"""
    if isinstance(obj, dict):
        return obj
    for meth in ("todict", "as_dict", "dict"):
        f = getattr(obj, meth, None)
        if callable(f):
            try:
                d = f()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    return {k: getattr(obj, k, None)
            for k in ("song_name", "singers", "album", "duration", "duration_s",
                      "file_size_bytes", "download_url", "ext", "bitrate",
                      "samplerate", "source", "identifier")}


def quality_score(track: dict) -> int:
    """按「有效码率（体积/时长）」为主、格式为辅估算音质。

    为什么不用 file_size_bytes 直接比大小：实测同一首《稻香》里
      aac 2.6MB / 3:09 = 110kbps   ← 体积小但其实是低码率
      mp3 8.5MB / 3:17 = 345kbps   ← 体积大且确实更好
      flac 68.9MB / 2:00 = 4.6Mbps ← 无损
    只比绝对体积会把「短而无损」排到「长而高码率」前面，所以必须除以时长。
    """
    ext = str(track.get("ext", "")).lower()
    if ext in ("flac", "ape", "wav"):
        base = 60
    elif ext in ("m4a", "aac"):
        base = 25
    else:                        # mp3 / ogg / 未知
        base = 20

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    size = _num(track.get("file_size_bytes"))
    dur = _num(track.get("duration_s")) or _num(track.get("duration"))
    kbps = (size * 8 / dur / 1000.0) if (size > 0 and dur > 0) else 0.0

    if kbps >= 800:
        base += 40
    elif kbps >= 500:
        base += 35
    elif kbps >= 300:
        base += 28
    elif kbps >= 240:
        base += 22
    elif kbps >= 190:
        base += 16
    elif kbps >= 128:
        base += 10
    elif kbps > 0:
        base += 2

    sr = _num(track.get("samplerate"))
    if sr >= 96000:
        base += 10
    elif sr >= 44100:
        base += 4
    if _num(track.get("bitrate")) >= 900000:
        base += 6
    return base


def build_client(sources, work_dir, search_size=10):
    cfg = {}
    for s in sources:
        cfg[s] = {
            "work_dir": str(Path(work_dir).expanduser().absolute()),
            "search_size_per_source": search_size,
        }
    return MusicClient(
        music_sources=sources,
        init_music_clients_cfg=cfg,
    )


def do_search(client, keyword, timeout=None):
    """搜索并返回展平后的候选列表（元素为 dict + _raw 原始对象）。

    用「守护线程 + join(timeout)」包一层：musicdl 的 search 在某个音源卡死时
    会一直等下去，这里最多等 timeout 秒就放弃并返回空。
    （不能用 ThreadPoolExecutor —— 它在解释器退出时会 join 非守护线程，
      卡死的线程仍会把进程拖住。）
    """
    import threading

    limit = SEARCH_TIMEOUT_S if timeout is None else int(timeout)
    box = {}

    def _worker():
        try:
            box["raw"] = client.search(keyword)
        except Exception as e:              # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(limit)

    if th.is_alive():
        print(f"  [超时] 搜索超过 {limit}s 未返回 —— "
              f"多半是某个音源的第三方接口不通（连接被挂起）。"
              f"可换音源重试，例如 --sources NeteaseMusicClient", file=sys.stderr)
        return []
    if "err" in box:
        print(f"  [异常] 搜索失败: {type(box['err']).__name__}: {box['err']}",
              file=sys.stderr)
        return []

    raw = box.get("raw")
    cands = []
    if not isinstance(raw, dict):
        return cands
    for src, items in raw.items():
        for it in (items or []):
            d = _as_dict(it)
            if not d:
                continue
            d = dict(d)
            d["source"] = d.get("source") or src
            d["_raw"] = it
            cands.append(d)
    return cands


def search_candidates(sources, keyword, workdir, size):
    """多源合并搜索；整体超时就**逐源降级**，能捞多少算多少。

    为什么值得这一步：合并搜索只要有一个源卡住就整体判超时、颗粒无收。
    逐源搜时每个源各算一份超时预算，最慢的源也不会拖垮其他源的结果。
    """
    cands = do_search(build_client(sources, workdir, size), keyword)
    if cands or len(sources) <= 1:
        return cands

    print(f"  [降级] 多源合并搜索未返回，改为逐源单独搜（单源上限 {SOURCE_TIMEOUT_S}s）",
          file=sys.stderr)
    merged = []
    for s in sources:
        got = do_search(build_client([s], workdir, size), keyword,
                        timeout=SOURCE_TIMEOUT_S)
        print(f"    {s:<24} -> {len(got)} 条", file=sys.stderr)
        merged.extend(got)
    return merged


def fmt_dur(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v or "?")
    return f"{int(v//60)}:{int(v%60):02d}"


def _sz_mb(d):
    try:
        return f"{float(d.get('file_size_bytes') or 0)/1048576:.1f}"
    except (TypeError, ValueError):
        return str(d.get("file_size") or "?")


def safe_name(s):
    """去掉文件名非法字符，避免入库后 MA 索引异常。"""
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(s or "")).strip().strip(". ")


def _dur_s(d):
    try:
        return float(d.get("duration_s") or 0)
    except (TypeError, ValueError):
        return 0.0


# 明显不是「原唱正片」的标记（与 auto_backfill.BAD_MARKS 保持一致）
# ⚠️ 教训（2026-09-13）：漏了器乐改编类标记，导致「周杰伦 七里香」下回了
#    「樱晚haruka - 【八音盒】周杰伦-七里香」——歌名里带着原唱歌手和歌名，
#    但演唱者完全不是本人。器乐/改编类标记必须单独列全。
BAD_MARKS = (
    # 翻唱 / 改编
    "cover", "翻唱", "remix", "dj", "深情版", "女声版", "男声版", "童声",
    "清唱", "acapella", "ai翻唱", "ai cover", "ai 翻唱",
    # 器乐 / 改编音色
    "八音盒", "音乐盒", "纯音乐", "器乐", "无人声", "off vocal",
    "钢琴", "吉他", "小提琴", "古筝", "二胡", "萨克斯", "电子琴", "口琴",
    "笛", "琵琶", "古琴", "竖琴", "instrumental", "变奏",
    # 片段 / 铃声 / 卡点
    "片段", "试听", "铃声", "高潮版", "卡点", "变速", "加速版", "慢版",
)

# ★ 加密 / DRM 封装格式（2026-09-13 实测新增）
#   教训：搜「周杰伦 七里香」时 rank_key 选中了一条 `mgg`（酷狗加密 ogg），
#   下载流程只落下一个 .lrc 歌词、没有音频，job 结果变成
#   `no_file_produced`（"只拿到歌词、没有音频"），白跑 3 分钟。
#   而且这些格式 Music Assistant 根本解不了 —— 就算下下来也播不了。
#   所以列为**硬拒收**（不是软偏好）。
ENCRYPTED_EXTS = (
    # 酷狗
    "mgg", "mgg1", "mggl", "mflac", "mflac0", "kgm", "kgma", "kgg", "vpr",
    # QQ 音乐
    "qmc0", "qmc2", "qmc3", "qmcflac", "qmcogg", "tkm", "mflac",
    # 网易云
    "ncm",
    # 虾米 / 咪咕
    "xm", "mg3",
)


def is_encrypted(cand) -> bool:
    """候选是不是加密/DRM 封装格式（下了也播不了，且常只给歌词）。"""
    ext = str(cand.get("ext") or "").strip().lower().lstrip(".")
    return ext in ENCRYPTED_EXTS


def relevance(cand, query):
    """
    候选与查询词的相关度，用于「原唱优先」。返回 0~100，越高越像要找的版本。

    ★ 繁简归一：查询与候选都先 to_simp() 再比，否则 GDStudio 这类返回
      「周杰倫」的音源会被整条漏判（实测命中数从 10 掉到 0）。
    """
    q = str(query or "")
    # 把查询拆成词元（中英文混排：按空格 + 常见分隔）
    toks = [t for t in q.replace("　", " ").split() if t]
    name = to_simp(cand.get("song_name") or "")
    singer = to_simp(cand.get("singers") or "")
    album = to_simp(cand.get("album") or "")
    score = 0
    for t in toks:
        tt = to_simp(t)
        if tt in singer:
            score += 50          # 歌手命中 —— 最强信号（原唱）
        elif tt in name:
            score += 25          # 歌名命中
        elif tt in album:
            score += 10
        else:
            # 该词既不在歌名也不在专辑里 → 极可能是「歌手/艺人」限定词。
            # 候选完全没命中它，说明这是翻唱或同名无关曲目 —— 强惩罚。
            # （实测教训：搜「周杰伦 晴天」曾误选「晴天 - 梦里啥都有」）
            score -= 40
    # 惩罚明显的翻唱/改编标记。
    # ★ 两条护栏：
    #   1) 标记词本身出现在**查询**里就不罚 —— 否则《吉他手》（陈绮贞）、
    #      《钢琴师》这类合法歌名会被自己的标题误杀。
    #   2) 罚得足够重（-60）—— 让「【八音盒】周杰伦-七里香」这类
    #      「歌名带全了歌手+歌名、但演唱者完全不是本人」的改编版
    #      直接落到选曲下限以下，而不是靠运气。
    q_low = to_simp(q).lower()
    lower = (name + " " + singer).lower()
    for mark in BAD_MARKS:
        if mark in lower and mark not in q_low:
            score -= 60
            break

    # ★ 加密/DRM 封装格式：一票否决（见 ENCRYPTED_EXTS 注释）。
    #   实测 mgg 只会落歌词、没有音频，且 MA 解不了。
    if is_encrypted(cand):
        score -= 100

    # ★ 「原唱 XXX」钓鱼式标题：查询词**全在歌名里、歌手一个都不沾**。
    #   实测踩坑：搜「周杰伦 稻香」时出现
    #     「稻香 (原唱 周杰伦) - 纪钧瀚 (Bryan Chi)」—— 标题里塞满原唱信息骗搜索，
    #     实际是别人的翻唱，还挂了 99:00 的假时长。这一条直接把它打到 0。
    ttoks = [to_simp(t) for t in toks]
    if len(ttoks) >= 2:
        in_name = [t for t in ttoks if t in name]
        in_singer = [t for t in ttoks if t in singer]
        if len(in_name) >= 2 and not in_singer:
            score -= 60

    # ★ 串烧：歌名里用 + / ＆ 拼了两首（如「晴天+稻香」），点单曲时不该给串烧。
    #   罚得比「相关度下限」重，确保精确点播时它不会顶掉原唱；
    #   查询词自己带 + 时不算（用户确实在找串烧）。
    if ("+" in name or "＋" in name or "＆" in name) and \
            not any(ch in q for ch in "+＋＆"):
        score -= 40

    # ★ 超长时长：单曲超过 15 分钟基本是「整张专辑/合集」被当成一首上传，
    #   实测「稻香 (原唱 周杰伦)」标称 99:00 就是这类。
    if _dur_s(cand) > 900:
        score -= 40

    return max(score, 0)


def cmd_search(args):
    use_json = bool(getattr(args, "json", False))
    # --json 模式下人类可读日志全部改走 stderr，保证 stdout 只有一行纯 JSON
    # （auto_backfill 就是靠这个拿候选，比刮表格可靠得多）
    _log = (lambda *a: print(*a, file=sys.stderr)) if use_json else print

    _log(f"[搜索] {args.query}   音源={args.sources}")
    cands = search_candidates(args.sources, args.query, args.workdir, args.size)
    if not cands:
        _log("  未找到任何结果")
        return 1
    # 与 cmd_fetch 用**同一个**排序键：保证「列表第 1 行」就是「实际会下载的那首」。
    # （否则出现过 auto_backfill 日志说「首选 稻香 (Live)」、实际下的是另一个版本）
    cands.sort(key=lambda c: rank_key(c, args.query))

    if use_json:
        out = []
        for c in cands:
            d = {k: v for k, v in c.items() if not str(k).startswith("_")}
            d["relevance"] = relevance(c, args.query)
            d["quality_score"] = quality_score(c)
            out.append(d)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    _log(f"\n找到 {len(cands)} 个候选（相关度优先，其次音质）：\n")
    _log(f"{'#':>3} {'相关':>4} {'音源':<20} {'歌名':<30} {'歌手':<18} "
         f"{'时长':>7} {'大小MB':>8} {'格式':>5}")
    _log("-" * 108)
    for i, c in enumerate(cands[: args.top]):
        _log(f"{i:>3} {relevance(c, args.query):>4} {str(c.get('source'))[:20]:<20} "
             f"{str(c.get('song_name'))[:30]:<30} "
             f"{str(c.get('singers'))[:18]:<18} {fmt_dur(c.get('duration_s')):>7} "
             f"{_sz_mb(c):>8} {str(c.get('ext')):>5}")
    return 0


# ---- 多候选选择（「丰富曲库」的核心）---------------------------------
# 版本后缀识别：『稻香 (Live)』『稻香（治愈版）』『稻香[Remix]』都归并成『稻香』。
# 否则一次点播「周杰伦 稻香」会把原唱、Live、治愈版各下一份 —— 三个副本，
# 而用户只想要一首。归并后：精确查询 = 1 首；歌手查询 = N 首不同歌。
_BRACKET_RE = re.compile(r"[（(\[【〔]([^）)\]】〕]{1,30})[）)\]】〕]")
_VER_KW = ("版", "live", "remix", "demo", "现场", "演唱会", "伴奏", "纯音乐",
           "instrumental", "cover", "翻唱", "重制", "重置", "修复", "串烧",
           "medley", "remaster", "acoustic", "不插电", "合唱版")
_ARTIST_SEP_RE = re.compile(r"[/&、,，;；]")


def strip_version(song):
    """去掉歌名里的版本后缀；括号内容不含版本关键词时原样保留。

    『晴天+稻香』（串烧但没写括号）不会被误伤 —— 它是另一个作品。
    """
    def _sub(m):
        inner = m.group(1).lower()
        return "" if any(k in inner for k in _VER_KW) else m.group(0)

    return _BRACKET_RE.sub(_sub, str(song or "")).strip()


def work_key(cand, keep_versions=False):
    """「同一作品」的归一化键 = (歌名, 主歌手)，繁简折叠 + 版本后缀归并后。

    用它去重而不是「歌名+音源」：
      * 同一首歌在 5 个音源各有一份 → 只保留最合适的一条，不塞 5 个重复文件
      * 「周杰伦&派伟俊」取第一位歌手 → 与「周杰伦」的同一首歌归并
      * keep_versions=True 时保留 Live/Remix 作为独立作品（想收藏版本时用）
    """
    song = cand.get("song_name") or ""
    if not keep_versions:
        song = strip_version(song)
    singer = _ARTIST_SEP_RE.split(str(cand.get("singers") or ""))[0]
    return (norm_cmp(song), norm_cmp(singer))


def _ext_ok(cand, allowed):
    return (not allowed) or str(cand.get("ext") or "").lower() in allowed


def rank_key(cand, query, allowed=None):
    """候选排序键（越小越优先）。

    ★ 唯一真源：`cmd_search`（人看的表格 / 给 auto_backfill 的 --json）和
      `cmd_fetch`（实际下载）都用这一个 —— 否则会出现「日志说首选 A、
      实际下载 B」这种自相矛盾。
    """
    return (
        -relevance(cand, query),
        not _ext_ok(cand, allowed),
        # 歌名越短越贴近查询。用**原始**歌名长度：『稻香』(2) 胜过
        # 『稻香 (Live)』(9)、『晴天+稻香』(5) —— 优先拿最"素"的那版。
        len(norm_cmp(cand.get("song_name"))),
        # 参与歌手越少越像原唱。实测「稻香 - 周杰伦, 派伟俊」（合唱/合作版）
        # 会靠更长的时长压过原唱，加这一档把它降下去。
        len([x for x in _ARTIST_SEP_RE.split(str(cand.get("singers") or "")) if x.strip()]),
        -quality_score(cand),
        -_dur_s(cand),
    )


def select_candidates(cands, query, pick, allowed_ext=None, rel_floor_ratio=0.6,
                      keep_versions=False):
    """按相关度选出 top-N 个**不同作品**。

    - 相关度下限 = max(最佳相关度 × ratio, 25)，避免为了凑数把不相关结果拉进来
    - allowed_ext 是**软偏好**：同相关度下优先选允许的格式，不做硬过滤。
      硬过滤的教训：`--max-ext mp3` 会把唯一命中歌手的原唱全剔掉，只剩翻唱，
      于是误下（见 2026-09-13 记录）。
    - allowed_ext 接受「逗号分隔字符串」或列表两种写法。
    """
    if not cands:
        return []
    # ★ 先剔掉加密/DRM 封装格式（mgg/ncm/qmc… 只给歌词，MA 也解不了）。
    #   必须在算 floor 之前剔，否则「唯一候选是加密格式」会把 floor 拉到 25，
    #   连带把其他正常候选也筛没了。
    cands = [c for c in cands if not is_encrypted(c)]
    if not cands:
        return []
    allowed = None
    if allowed_ext:
        if isinstance(allowed_ext, str):
            allowed = {e.strip().lower() for e in allowed_ext.split(",") if e.strip()}
        else:
            allowed = {str(e).strip().lower() for e in allowed_ext if str(e).strip()}
        allowed = allowed or None

    ranked = sorted(cands, key=lambda c: rank_key(c, query, allowed))
    floor = max(int(relevance(ranked[0], query) * rel_floor_ratio), 25)
    picked, seen = [], set()
    for c in ranked:
        if relevance(c, query) < floor:
            continue
        k = work_key(c, keep_versions)
        if k in seen:
            continue
        seen.add(k)
        picked.append(c)
        if len(picked) >= pick:
            break
    return picked


def pick_from_approved(cands, approved, pick, keep_versions=False):
    """按调用方（auto_backfill）**已经筛过的清单**选曲。

    ★ 为什么必须有这一步：下载层如果自己再搜再选，就绕过了决策层的所有守卫
      —— 实测 auto_backfill 明明标了「库里已有《晴天》」，cmd_fetch 却照下不误，
      库里于是出现两首《晴天》。决策层筛完就必须把结果传下来。

    匹配：先按 (歌名, 歌手) 全等，退化为只比歌名；同一首取音质最好的那条。
    一条都没匹配上就返回空，由调用方决定是否回退到自己选。
    """
    out, seen = [], set()
    # 加密/DRM 格式一律不参与匹配（见 ENCRYPTED_EXTS）
    cands = [c for c in cands if not is_encrypted(c)]
    for a in (approved or [])[:pick]:
        if not isinstance(a, dict):
            continue
        an = norm_cmp(a.get("song_name"))
        asg = norm_cmp(a.get("singers"))
        if not an:
            continue
        matches = [c for c in cands
                   if norm_cmp(c.get("song_name")) == an
                   and norm_cmp(c.get("singers")) == asg]
        if not matches:
            matches = [c for c in cands if norm_cmp(c.get("song_name")) == an]
        if not matches:
            continue
        matches.sort(key=lambda c: (len(norm_cmp(c.get("song_name"))),
                                    -quality_score(c), -_dur_s(c)))
        c = matches[0]
        k = work_key(c, keep_versions)
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def probe_duration(p):
    """ffprobe 取时长（秒）；失败返回 None。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return round(float(r.stdout.strip()), 2)
    except Exception as e:                       # noqa: BLE001
        print(f"  ffprobe 失败 {Path(p).name}: {e}")
    return None


def unique_dst(outdir, stem, suffix, source=None):
    """给目标文件取一个不冲突的名字。

    同名时先试「歌手 - 歌名 [音源].ext」（能看出是哪来的），再退化为编号。
    """
    dst = outdir / f"{stem}{suffix}"
    if not dst.exists():
        return dst
    if source:
        alt = outdir / f"{stem} [{safe_name(source)}]{suffix}"
        if not alt.exists():
            return alt
    i = 1
    while True:
        cand = outdir / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def download_one(client, cand, tmpdir, wait_s=20.0):
    """下载单个候选，返回 (新文件列表, 错误串)。

    用「目录快照 diff」判断产出 —— musicdl 的 download() 返回值不可靠，
    但文件一定会落在 work_dir 下。
    """
    before = set(tmpdir.rglob("*"))
    err = ""
    try:
        client.download([cand["_raw"]])
    except Exception as e:                       # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    new = []
    if not err:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(0.5)
            after = set(tmpdir.rglob("*"))
            new = [p for p in (after - before) if p.is_file()
                   and p.suffix.lower() in AUDIO_EXTS + (".lrc",)]
            if new:
                break
    return new, err


def cmd_fetch(args):
    outdir = Path(args.out).expanduser().absolute()
    if not outdir.exists():
        print(f"[错误] 输出目录不存在: {outdir}")
        return 1
    if not os.access(outdir, os.W_OK):
        print(f"[错误] 输出目录不可写: {outdir}")
        if str(outdir).startswith("/mnt/music"):
            print("       /mnt/music 仍以 ro 挂载。修复：")
            print("         sudo bash outputs/fix_nfs_rw.sh")
            print("       （群晖侧需先把 NFS 权限设为「可读写」）")
        return 1

    tmpdir = Path(args.tmp or "/tmp/musicdl_stage").expanduser().absolute()
    tmpdir.mkdir(parents=True, exist_ok=True)

    client = build_client(args.sources, tmpdir, args.size)
    print(f"[搜索] {args.query}   音源={args.sources}")
    cands = search_candidates(args.sources, args.query, tmpdir, args.size)
    if not cands:
        print("  未找到任何结果")
        return 1

    pick = max(1, min(int(getattr(args, "pick", 1) or 1), 20))
    keep_ver = bool(getattr(args, "keep_versions", False))

    approved = []
    ap_path = getattr(args, "approved", None)
    if ap_path:
        try:
            approved = json.loads(Path(ap_path).read_text(encoding="utf-8"))
        except Exception as e:                   # noqa: BLE001
            print(f"  [!] 读 --approved 失败：{e}，退回自己选", file=sys.stderr)

    # ★ 调用方给了「已筛清单」就一律听它的 —— 那是决策层（auto_backfill）
    #   查过本地库、过过守卫的结果。自己重选会绕过那些守卫。
    picked = pick_from_approved(cands, approved, pick, keep_ver)
    if picked:
        print(f"\n[选曲] 按调用方已筛清单下载 {len(picked)} 条（共 {len(cands)} 条候选）：")
    else:
        if approved:
            print("  [!] 已筛清单一条都没匹配上（两次搜索结果不一致），退回自己选",
                  file=sys.stderr)
        picked = select_candidates(cands, args.query, pick, args.max_ext,
                                   keep_versions=keep_ver)
        if picked:
            print(f"\n[选曲] 共 {len(cands)} 条候选，按相关度选出 {len(picked)} 条不同作品"
                  f"（--pick {pick}{'，保留版本差异' if keep_ver else ''}）：")
    if not picked:
        print("  没有相关度达标的候选（可能全是翻唱/同名曲）")
        return 1

    print(f"\n[选曲] 共 {len(cands)} 条候选，按相关度选出 {len(picked)} 条不同作品"
          f"（--pick {pick}{'，保留版本差异' if keep_ver else ''}）：")
    for i, c in enumerate(picked, 1):
        print(f"  {i}. 相关度={relevance(c, args.query):>3}  "
              f"{c.get('song_name')} - {c.get('singers')}  "
              f"({fmt_dur(c.get('duration_s'))}, {c.get('source')}, "
              f"{c.get('ext')}, 音质分={quality_score(c)})")

    best = picked[0]
    if relevance(best, args.query) < 25:
        print("[警告] 相关度偏低 —— 未命中查询里的歌手/专辑限定词，"
              "很可能是翻唱或同名曲。建议先用 search 子命令人工挑一首。")

    # ---- 逐条下载 ----
    print(f"\n[下载] -> {tmpdir}")
    got = []
    for i, c in enumerate(picked, 1):
        files, err = download_one(client, c, tmpdir)
        tag = f"{c.get('song_name')} - {c.get('singers')}"
        if err:
            print(f"  [{i}/{len(picked)}] {tag}  !! {err}")
        else:
            print(f"  [{i}/{len(picked)}] {tag}  -> {len(files)} 个文件")
        got.append({"cand": c, "files": files, "error": err})

    if not any(g["files"] for g in got):
        print("  !! 全部未产出文件，列出目录内容：")
        for p in sorted(tmpdir.rglob("*"))[-15:]:
            print("   ", p)
        return 2

    # ---- 校验时长 + 改名入库 ----
    min_dur = int(getattr(args, "min_duration", 0) or 0)
    report = {"query": args.query, "sources": list(args.sources), "pick": pick,
              "picked": [{"song_name": c.get("song_name"), "singers": c.get("singers"),
                          "source": c.get("source"), "ext": c.get("ext"),
                          "relevance": relevance(c, args.query)} for c in picked],
              "files": [], "moved_to": [], "rejected_short": [], "failed": []}

    for g in got:
        c = g["cand"]
        tag = f"{c.get('song_name')} - {c.get('singers')}"
        if g["error"]:
            report["failed"].append({"song": c.get("song_name"),
                                     "singers": c.get("singers"),
                                     "source": c.get("source"),
                                     "error": g["error"]})
            continue

        audio = [p for p in g["files"] if p.suffix.lower() in AUDIO_EXTS]
        lyrics = [p for p in g["files"] if p.suffix.lower() == ".lrc"]
        if not audio:
            report["failed"].append({"song": c.get("song_name"),
                                     "singers": c.get("singers"),
                                     "source": c.get("source"),
                                     "error": "no_audio_file"})
            print(f"  !! {tag} 只拿到歌词、没有音频")
            continue

        singer = str(c.get("singers") or "").split("/")[0].strip()
        song = str(c.get("song_name") or "").strip()
        stem = safe_name(f"{singer} - {song}" if singer else song) \
            or safe_name(c.get("identifier") or "unknown")

        for p in audio:
            dur = probe_duration(p)
            mb = p.stat().st_size / 1048576
            entry = {"file": p.name, "size_mb": round(mb, 1), "duration_s": dur,
                     "source": c.get("source"), "song": song, "singers": singer,
                     "ext": p.suffix.lstrip("."), "path": str(p)}
            # 时长门槛：比标称短太多 / 疑似 60 秒试听截断 → 不进库
            if min_dur and dur is not None and dur < min_dur:
                entry["reason"] = "duration_%ss_lt_%ds" % (dur, min_dur)
                report["rejected_short"].append(str(p))
                print(f"  [拒] {p.name}  时长 {fmt_dur(dur)} < {min_dur}s")
                continue
            if dur is not None and 55 <= dur <= 65 and _dur_s(c) > 120:
                entry["warn"] = "疑似 60 秒试听截断"
            if not args.no_move:
                dst = unique_dst(outdir, stem, p.suffix, c.get("source"))
                try:
                    shutil.move(str(p), str(dst))
                    entry["moved_to"] = str(dst)
                except Exception as e:           # noqa: BLE001
                    entry["error"] = "move_failed: %s" % e
                    report["failed"].append(entry)
                    print(f"  !! 移动失败 {p.name}: {e}")
                    continue
            report["files"].append(entry)
            if entry.get("moved_to"):
                report["moved_to"].append(entry["moved_to"])
            flag = "  <<< " + entry["warn"] if entry.get("warn") else ""
            print(f"  [OK] {p.name}  {mb:.1f}MB | {fmt_dur(dur)} | {p.suffix}{flag}")

        # 歌词跟随音频一起入库（同名前缀）
        for p in lyrics:
            if args.no_move:
                continue
            try:
                shutil.move(str(p), str(unique_dst(outdir, stem, p.suffix)))
            except Exception:                    # noqa: BLE001
                pass

    rp = Path(args.report or f"/tmp/musicdl_report_{int(time.time())}.json")
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[汇总] 入库 {len(report['moved_to'])} 个文件"
          + (f"，时长不足拒收 {len(report['rejected_short'])} 个" if report["rejected_short"] else "")
          + (f"，失败 {len(report['failed'])} 条" if report["failed"] else ""))
    print(f"[报告] {rp}")

    if not report["moved_to"] and not args.no_move:
        print("[结果] 没有任何文件入库")
        return 2

    if not args.no_move:
        print("\n[下一步] 让 MA 立即收录新文件（否则等下次 12 小时增量同步）：")
        print("  方式1（推荐，走 MA API）：")
        print("    python3 outputs/rescan_library.py")
        print("  方式2（MA Web UI）：设置 → 音乐库 → 立即同步")

    return 0


def cmd_batch(args):
    qf = Path(args.query_file).expanduser()
    if not qf.exists():
        print(f"[错误] 查询文件不存在: {qf}")
        return 1
    queries = [ln.strip() for ln in qf.read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
    print(f"[批量] {len(queries)} 条查询\n")
    ok = fail = 0
    for i, q in enumerate(queries, 1):
        print(f"\n{'='*66}\n[{i}/{len(queries)}] {q}\n{'='*66}")
        ns = argparse.Namespace(**vars(args), query=q)
        rc = cmd_fetch(ns)
        if rc == 0:
            ok += 1
        else:
            fail += 1
            print(f"  !! 失败（rc={rc}）")
    print(f"\n[批量完成] 成功 {ok} / 失败 {fail} / 合计 {len(queries)}")
    return 0 if fail == 0 else 2


def main():
    ap = argparse.ArgumentParser(description="musicdl 落地脚本：搜索 / 下载 / 校验 / 入库")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = dict()
    for name in ("search", "fetch"):
        p = sub.add_parser(name)
        p.add_argument("query", help="搜索关键词，如 '周杰伦 晴天'")
        p.add_argument("--sources", nargs="+", default=DEFAULT_SOURCES,
                       help=f"音源列表，默认 {DEFAULT_SOURCES}")
        p.add_argument("--size", type=int, default=10, help="每音源搜索结果数")
        p.add_argument("--top", type=int, default=20, help="search 模式展示条数")
        p.add_argument("--max-ext", help="限制格式（逗号分隔），如 mp3 / mp3,m4a；"
                                         "用于控制磁盘占用（FLAC 约 150MB/首，MP3 约 12MB/首）")
        if name == "search":
            p.add_argument("--json", action="store_true",
                           help="stdout 输出一行 JSON 候选数组（日志走 stderr）。"
                                "给 auto_backfill 这类调用方用，比刮表格可靠")
        if name == "fetch":
            p.add_argument("--out", default=DEFAULT_OUT,
                           help=f"音乐库输出目录，默认 {DEFAULT_OUT}（群晖）")
            p.add_argument("--tmp", default="/tmp/musicdl_stage", help="下载暂存目录")
            p.add_argument("--no-move", action="store_true", help="不移动到音乐库")
            p.add_argument("--report", help="JSON 报告输出路径")
            p.add_argument("--pick", type=int, default=1,
                           help="下载几条**不同作品**（按相关度取，同一歌名+歌手只算一条）。"
                                "默认 1 = 只补这一首；>1 用于丰富曲库，如 3")
            p.add_argument("--keep-versions", action="store_true",
                           help="把 Live/Remix/治愈版等视为独立作品一并下载"
                                "（默认会归并到同一首，避免一次下回多个副本）")
            p.add_argument("--approved", help="调用方已筛好的清单 JSON 路径"
                                              "[{song_name, singers}, ...]。"
                                              "给了就按它下载，不再自己选")
            p.add_argument("--min-duration", type=int, default=0,
                           help="入库最短时长（秒），低于此值不进库（留在暂存目录）；"
                                "0 = 不限制（由 auto_backfill 统一把关）")
            p.set_defaults(func=cmd_fetch, workdir="/tmp/musicdl_stage")
        else:
            p.set_defaults(func=cmd_search, workdir="/tmp/musicdl_stage")

    pb = sub.add_parser("batch")
    pb.add_argument("query_file", help="查询文件，每行一个关键词")
    pb.add_argument("--sources", nargs="+", default=DEFAULT_SOURCES)
    pb.add_argument("--size", type=int, default=10)
    pb.add_argument("--top", type=int, default=20)
    pb.add_argument("--max-ext", help="限制格式（逗号分隔），如 mp3")
    pb.add_argument("--out", default=DEFAULT_OUT, help=f"输出目录，默认 {DEFAULT_OUT}")
    pb.add_argument("--tmp", default="/tmp/musicdl_stage")
    pb.add_argument("--no-move", action="store_true")
    pb.add_argument("--report")
    pb.add_argument("--pick", type=int, default=1, help="每条查询下载几条不同作品")
    pb.add_argument("--min-duration", type=int, default=0, help="入库最短时长（秒）")
    pb.set_defaults(func=cmd_batch, workdir="/tmp/musicdl_stage")

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
