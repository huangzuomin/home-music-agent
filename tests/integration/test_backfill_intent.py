"""T01 / T02 / T03 —— 延迟补库不得改写播放意图（T01/T03 当前为缺陷，xfail）。

对应 review：G01。根因（源码级）：
auto_backfill.run() 在完成路径 `if args.play: ha_play_uri(...)` **无条件**播放，
且 music_fetch 工具硬编码 play=True——用户点 B / 停止之后，迟到的 A 补库
完成仍会把音乐抢换成 A（或让停止的音乐复活）。
修复归属 IMP-02（阻断自动播放、默认仅入库）。

驱动方式：auto_backfill.run() 的全部外部边界（MA 查库/在线候选/下载/重扫/
ha_play_uri）由 FakeFetcher 打桩；「补库完成」的时刻由测试显式调用 run() 模拟。
"""
from __future__ import annotations

import pytest

from tests.fakes.fake_fetcher import FakeFetcher

QUERY_A = "周杰伦 稻香"
URI_A = "library://track/99"


@pytest.fixture()
def fetcher(monkeypatch, tmp_path):
    fake = FakeFetcher(monkeypatch,
                       __import__("auto_backfill"), tmp_path)
    fake.add_candidate("稻香", "周杰伦")                      # 在线候选（守卫全过）
    # 下载并重扫后，A 才出现在库里（本地原本没有 → 走在线补库）
    fake.post_sync.append({"uri": URI_A, "name": "稻香", "singers": "周杰伦",
                           "duration": 225.0, "artists": [{"name": "周杰伦"}]})
    return fake


def test_t01_backfill_completing_after_b_plays__must_not_steal(fetcher):
    """IMP-02 已修复：完成通道只报告 asset_ready，不再触发播放。"""
    """用户点 A（A 转入补库）后改点 B；A 补库完成时不得把 B 换成 A。"""
    args = fetcher.make_args(QUERY_A, play=True)
    result = fetcher.ab.run(args)

    # 补库与入库本身应当成功（这是被复用的合法部分）
    assert result["ok"], result.get("reason")
    assert result["files"], "下载入库应产出文件（%s）" % result.get("reason")

    # 契约：A 完成后**只入库**，当前正在播放的 B 不被抢换
    assert fetcher.plays == [], (
        "补库完成不应触发播放（当前抢播了 %r）" % fetcher.plays)
    assert result.get("asset_ready") is True, "应报告资源就绪"
    assert result.get("autoplay_blocked") is True, "应登记 autoplay 请求被阻断"


def test_t02_stop_then_backfill_completes__stays_stopped(fetcher):
    """IMP-02 已修复：用户停止后，迟到的补库完成不让音乐复活。"""
    """用户停止播放后，迟到的补库完成不得让音乐复活。"""
    fetcher.stopped = True                    # 用户已停止（T02 前提）
    args = fetcher.make_args(QUERY_A, play=True)
    result = fetcher.ab.run(args)

    assert result["ok"], result.get("reason")
    assert fetcher.plays == [], (
        "已停止后补库完成不应触发播放（实际播放了 %r）" % fetcher.plays)


def test_t03_fetch_tool_import_only__playback_untouched(fetcher):
    """IMP-02 已修复：工具链的「仅入库」请求全程不触发播放。"""
    """通过真实工具链（music_fetch 计划 → run）验证「仅入库不改变播放」。

    当前 tools.plan 的 music_fetch 硬编码 play=True → 完成即播放。
    """
    plan_tools = __import__("tools")
    plan = plan_tools.plan("music_fetch", {"query": QUERY_A})

    args = fetcher.make_args(
        QUERY_A, play=bool(plan["variables"].get("play", False)))
    result = fetcher.ab.run(args)

    assert result["ok"], result.get("reason")
    assert fetcher.plays == [], (
        "仅入库请求不应触发播放（实际播放了 %r）" % fetcher.plays)


def test_t03_companion_import_with_play_false__downloads_without_playing(fetcher):
    """正向契约：run(play=False) 已支持「只下载入库、不播放」——
    IMP-02 的修复就是把自动路径收敛到这一行为。"""
    args = fetcher.make_args(QUERY_A, play=False)
    result = fetcher.ab.run(args)

    assert result["ok"], result.get("reason")
    assert result["files"], "应产出入库文件"
    assert fetcher.plays == [], "play=False 时不得播放"
    assert fetcher.sync_calls >= 1, "入库后应触发 MA 重扫"
