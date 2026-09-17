# IMP-02 部署切换检查单（cutover checklist）

> ⚠️ 本清单是**部署授权窗口**内的执行单。代码已完成（本仓库），但按计划
> §0.3，实际执行需要既定的部署授权；未走完本清单不得宣布 IMP-02 上线。

## 0. 窗口前置

- [ ] 授权窗口确认（建议 ≤60 分钟，家庭成员已知情）
- [ ] 备份：`/opt/home-music-agent/data/sessions/`、`services/music-fetcher/data/`、
      `config/home-assistant/`（一致快照；不要在运行中裸拷 SQLite）
- [ ] 记录当前镜像 id（docker inspect）与 HA 包版本，作为回滚锚点
- [ ] 回滚锚点验证：能检出上一个可用提交并重新构建

## 1. 关闭旧任务的「新接入」

- [ ] 停止每日充实 automation（HA 侧 `automation.***`，03:30 那条）
- [ ] 确认 fetcher 队列已排空：`curl -s :8300/jobs | python3 -m json.tool`
      （存在 queued/running 作业时等待完成或按 §3 隔离）

## 2. 隔离旧 fetcher 子进程

- [ ] `ps -ef | grep musicdl_fetch` —— 确认没有运行中的下载子进程
- [ ] 如有：等待其自然结束（不强杀；下载中的临时文件按 reject 流程处理）
- [ ] `docker logs music-fetcher --tail 50` 复核无「回调播放」日志

## 3. HA 长脚本核查

- [ ] 开发者工具 → 状态：`script.music_play_query / music_play_uri /
      music_filler` 无 `on` 状态的卡住脚本
- [ ] 如有卡住：等待结束；`script.turn_off` 仅在确认其正在写播放器时使用

## 4. 部署新版本

- [ ] `git pull`（或同步代码）到 `/opt/home-music-agent`
- [ ] `docker compose build voice-gateway music-fetcher && docker compose up -d voice-gateway music-fetcher`
- [ ] HA 包更新后 reload：script + rest_command（各自独立）

## 5. 上线后核验（对照 T 用例）

- [ ] T02：点一首库外歌 → 立即停止 → 补库完成后音乐**保持停止**
- [ ] T03：补库完成只入库 → 当前曲与进度不变、`/jobs` 显示 asset_ready
- [ ] T15：说「闭麦」→ 回复「请使用设备上的关闭键」，音乐**继续播放**
- [ ] T17：静音 → 取消静音 → 恢复的是静音前的音量（有记录时）
- [ ] T12：连续两次不同点赞 → 画像 signal_counts 各计一次

## 6. 回滚（仅在阻断性故障时）

- [ ] 回退应用与 HA 包到步骤 0 记录的锚点
- [ ] ⚠️ **不得通过回滚重新打开自动抢播**——回滚版本也必须保留
      「补库完成只入库」的安全行为
- [ ] 回滚后复核：无运行中旧子进程、无卡住的 HA 长脚本

## 7. 收尾

- [ ] 更新 `risk-register.md`：R-NEW-* 状态、本窗口的实际执行记录
- [ ] 家庭成员告知：语音入口当前无唤醒词（VAD 调试模式，琥珀灯），房间内
      对话可能被采集为指令
