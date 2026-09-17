"""FakeFetcher：把 auto_backfill.run() 的全部外部边界打桩，离线驱动补库流程。

可注入：在线候选、下载结果、本地库状态、用户已停止（T02）。
`plays` 记录 ha_play_uri 调用（补库完成通道的播放）——T01/T02/T03 的断言点。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


class FakeFetcher:
    def __init__(self, monkeypatch, ab_mod, tmp_path: Path) -> None:
        self.ab = ab_mod
        self.tmp = Path(tmp_path)
        self.library: list[dict] = []      # 同步前的 MA 本地库
        self.post_sync: list[dict] = []    # ma_sync 之后出现的入库曲目
        self.synced = False
        self.candidates: list[dict] = []   # 在线候选（解析后的形状）
        self.downloads: list[tuple[str, float]] = []  # (曲名, 时长秒)
        self.plays: list[str] = []         # ha_play_uri 记录（完成通道的播放）
        self.sync_calls = 0
        self.fetch_calls: list[str] = []

        monkeypatch.setattr(ab_mod, "MA_TOKEN", "test-token")
        monkeypatch.setattr(ab_mod, "REJECT_DIR", str(self.tmp / "reject"))
        def _library_tracks():
            if self.synced:
                return [dict(t) for t in self.library + self.post_sync]
            return [dict(t) for t in self.library]
        monkeypatch.setattr(ab_mod, "library_tracks", _library_tracks)
        monkeypatch.setattr(
            ab_mod, "online_candidates",
            lambda query, sources, size=10, timeout=None:
                (0, json.dumps(self.candidates), ""))
        monkeypatch.setattr(ab_mod, "parse_candidates_json",
                            lambda text: list(self.candidates))
        monkeypatch.setattr(ab_mod, "do_fetch", self._do_fetch)
        monkeypatch.setattr(ab_mod, "read_report", self._read_report)
        def _ma_sync(providers=None):
            self.sync_calls += 1
            self.synced = True
        monkeypatch.setattr(ab_mod, "ma_sync", _ma_sync)
        monkeypatch.setattr(ab_mod, "ha_play_uri", self._ha_play_uri)

    # ---- 打桩的边界 ----
    def _do_fetch(self, query, sources, outdir, report_path, timeout=1500,
                  pick=1, min_duration=0, approved=None):
        self.fetch_calls.append(query)
        Path(report_path).write_text("{}", encoding="utf-8")
        return 0, "downloaded"

    def _read_report(self, report_path):
        moved = []
        durations = []
        for name, dur in self.downloads:
            p = self.tmp / f"{name}.mp3"
            p.write_bytes(b"fake-audio")
            moved.append(str(p))
            durations.append(dur)
        return moved, [], durations

    def _ha_play_uri(self, uri):
        self.plays.append(uri)
        return True, ""

    # ---- 测试辅助 ----
    def make_args(self, query: str, play: bool = True, pick: int = 1,
                  dry_run: bool = False):
        return argparse.Namespace(
            query=query, pick=pick, play=play, dry_run=dry_run,
            sources="Fake", out=str(self.tmp / "dl"), timeout=30,
            min_duration=60)

    def add_candidate(self, song: str, singer: str) -> None:
        """构造能通过全部守卫的候选（**解析后**形状：dur 为 m:ss 字符串）。"""
        self.candidates.append({
            "idx": 0,
            "source": "Fake",
            "song_name": song,
            "singers": singer,
            "dur": "3:45",
            "size_mb": 8.5,
            "ext": "mp3",
            "relevance": 100,
        })
        self.downloads.append((song, 225.0))   # 该候选会被下载产出

    def add_library_track(self, uri: str, song: str, singer: str,
                          duration: float = 225.0) -> None:
        self.library.append({"uri": uri, "name": song, "singers": singer,
                             "duration": duration,
                             "artists": [{"name": singer}]})
