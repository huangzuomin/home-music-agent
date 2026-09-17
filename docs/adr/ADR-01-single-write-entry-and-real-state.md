# ADR-01：播放器真实状态与单一写入口

状态：已采纳（IMP-03a）
日期：2026-09-17

## 背景

Music Assistant（MA）是家庭播放器（Squeezebox、Cast 等）的直接管理者，掌握
队列、曲目与播放进度的真实状态。本系统（voice-gateway + PWA + 语音客户端）
会代表用户向 MA 发起写操作（播放/暂停/切歌/音量/队列）。

此前网关把动作翻译成 HA script（music_play_query/music_next/...）后即返回
`ok=True`——HTTP 受理被当成播放成功（G03/T09），且存在多条各自独立的写路径
（补库回调、每日任务、HA 自动化），彼此没有顺序与失效约定（G01/T01/T02）。

## 决定

1. **MA 是播放器真实状态的唯一事实来源。** 本系统的数据库不保存第二份
   now_playing；快照只是带 freshness 的缓存（`playback_state.py`）。
2. **单一写入口。** 所有系统管理的 MA 写操作经由
   `CommandCoordinator → ha_executor → script.music_execute_v1 → rest_command.ma_call`
   这一条链路（IMP-03c）。旧脚本（music_next/music_pause/...）迁移后停用或
   降级为不含独立策略的薄适配；补库完成回调不再触发播放（IMP-02 已阻断）。
3. **本库不与 MA 争夺播放器真实状态。** 外部人工控制（遥控/MA UI）优先；
   检测到非受管修改时，相关自动计划转 `suspended`，不抢回。
4. **写操作的执行确认以观测为准。** HA 受理（200）只标记 accepted/queued；
   `player_confirmed` 需要事后从 MA 读到与动作匹配的状态（计划 §4.2）。

## 后果

- 命令、授权、会话进 SQLite（本系统状态）；播放快照只是缓存——两者分清。
- 网关单写进程是前提（计划 §4.6）；多 worker 需另立协调设计。
- 旧脚本在迁移完成前作为安全适配器运行，不允许携带自己的策略。
