#!/usr/bin/env python3
"""整理 Music Assistant 的播放清单（读 MA 的 playlog 表）。

数据源：MA 容器内 /data/library.db 的 playlog 表。
  - playlog.timestamp 是**播放结束时刻**（epoch 秒），不是开始时刻；
    开始时刻 = timestamp - seconds_played。可用相邻曲目的首尾相接来验证。
  - playlog 会同时记录 media_type='artist' 的队列项，属于噪音，需过滤。

⚠️ 库被 MA 独占，直接读会 `database is locked`；本脚本会自动复制副本再读。

在 VM1 上的调用方式（脚本本身在 MA 容器内执行）：
  ssh your-vm 'docker exec -i music-assistant python3 - 2 \
      < /opt/home-music-agent/services/music-assistant/tools/play_history.py'

参数： argv[1]=小时数（默认 2）；argv[2]=db 路径（默认 /data/library.db）
"""
import datetime
import json
import shutil
import sqlite3
import sys

TZ = datetime.timezone(datetime.timedelta(hours=8))


def open_db(path):
    try:
        c = sqlite3.connect(path)
        c.execute("select count(*) from playlog").fetchone()
        return c
    except sqlite3.OperationalError:
        tmp = "/tmp/play_history_copy.db"
        shutil.copy(path, tmp)
        for suffix in ("-wal", "-shm"):
            try:
                shutil.copy(path + suffix, tmp + suffix)
            except OSError:
                pass
        return sqlite3.connect(tmp)


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    db = sys.argv[2] if len(sys.argv) > 2 else "/data/library.db"

    now = datetime.datetime.now(TZ)
    start = now - datetime.timedelta(hours=hours)
    c = open_db(db)

    rows = list(c.execute(
        """select item_id, name, artists, timestamp, fully_played, seconds_played
             from playlog
            where timestamp >= ? and media_type = 'track'
            order by timestamp asc""",
        (start.timestamp(),),
    ))

    ts = lambda t: datetime.datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S")
    print("窗口 %s ~ %s  |  %d 首\n" % (
        start.strftime("%Y-%m-%d %H:%M"), now.strftime("%H:%M"), len(rows)))

    total = 0
    for i, (item_id, name, arts, end, fully, secs) in enumerate(rows, 1):
        try:
            who = ", ".join(x.get("name", "") for x in (json.loads(arts) or []))
        except Exception:
            who = ""
        secs = secs or 0
        total += secs
        flag = "" if fully else "  [未播完]"
        print("%2d. %s-%s  %-34s | %-20s | %3d:%02d%s" % (
            i, ts(float(end) - secs), ts(float(end)), (name or "")[:34],
            who[:20], secs // 60, secs % 60, flag))

    print("\n合计 %d 分 %d 秒（%.1f 小时）" % (total // 60, total % 60, total / 3600))


if __name__ == "__main__":
    main()
