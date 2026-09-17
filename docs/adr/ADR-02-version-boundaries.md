# ADR-02：intent_epoch / queue_revision / event_seq 的边界

状态：已采纳（IMP-03a）
日期：2026-09-17

## 背景

命令失效、队列撤销与前端推送对账是三个不同的语义，若共用一个自增计数会
产生「一次重连把播放意图推进一代」或「一次重排让推送游标失效」之类的
串扰。计划 §4.1 明确三者不得混用。

## 决定

| 版本号 | 语义 | 谁推进 | 用途 |
| --- | --- | --- | --- |
| `intent_epoch` | 播放意图代际 | 服务端：立即替换、停止、会话结束、显式取消、暂停（使未执行自动计划失效）、不可归因的外部接管 | 判定旧计划/迟到结果是否失效（superseded） |
| `queue_revision` | 队列内容/顺序/游标的语义修订 | 受管队列修改（IMP-10 的 queue_patch）或确认过的 MA 队列变化 | 未来队列调整与撤销的一致性 |
| `event_seq` + `server_boot_id` | 前端事件推送游标 | 网关每次推送递增；重启换 boot_id | SSE 断流重连对账，检测事件缺口 |

补充规则：

1. 普通时间流逝、播放进度跳动**不推进** intent_epoch，也不算 queue_revision。
2. 音量调整**不影响** intent_epoch/queue_revision，也**不取消**定时
   （timer_revision 另立，IMP-11）。
3. 命令行上 `expected_queue_revision` 不匹配 → REVISION_CONFLICT，不套用旧计划。
4. `event_seq` 不能当播放授权：前端重连后必须重新拉快照，而不是重放本地命令。
