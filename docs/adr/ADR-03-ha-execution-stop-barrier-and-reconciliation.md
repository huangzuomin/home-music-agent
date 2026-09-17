# ADR-03：HA 执行确认、STOP 屏障、超时与重启对账

状态：已采纳（IMP-03c）
日期：2026-09-17

## 背景

命令从 HTTP 受理到扬声器出声跨越三个系统（网关 → HA script → MA → 播放器）。
此前网关在 HA 返回 200 时即返回 `ok=True`（受理被当成播放成功，T09），且
存在多条独立写路径，彼此无顺序约定（G01/T01/T02：迟到的补库回调抢播）。
另外 HA 的 `script.turn_on` 不等待脚本完成——进程内加锁并不能把串行延伸到
HA 执行边界（计划 §4.4 明确禁止这种伪串行）。

## 决定

### 1. 统一短执行器（script.music_execute_v1）

所有系统管理的 MA 写操作经同一 HA 脚本下发。脚本只接收**已解析的短动作**：

```yaml
fields: ma_command（MA 命令名）、ma_args_json（参数 JSON 字符串，由网关
        预序列化——规避 Jinja 布尔渲染坑）、command_id（对账键）
sequence: rest_command.ma_call
```

慢搜索、模型推理、资源准备**不进入**本脚本；结果就绪后由网关重新校验
intent_epoch / 目标 / queue_revision 再发起新命令。

### 2. 生命周期与确认分级

```
accepted → resolving(可选) → queued → executing → player_confirmed
                                              ↘ failed / superseded / unknown
```

- executor 回执成功 → `player_confirmed=True`（v1：收据级确认，适用于
  stop/next/volume 等确定性单写动作；回执失败 → failed）。
- 快照级确认（对照 playback_state）由 IMP-08 接入确认器完善。
- 无回执/超时 → `unknown`：对账流程负责，不自动重试非幂等动作（next/previous）。

### 3. STOP 屏障

STOP 到达时按序执行：

1. `supersede_pending(epoch)`：当前代际下所有 accepted/resolving/queued
   命令标记 superseded（未派发的旧计划不再派发）；
2. 推进 `intent_epoch`（写回 meta）；
3. 清空协调器未派发队列；
4. 执行 stop（排在已派发动作之后——同步执行天然保证顺序）。

在途最小写动作不能被虚构取消：若执行结果未知，状态标 unknown 并如实提示
「执行结果未知，请查看播放器」，同时暂停自动播放，不承诺绝对停止。

### 4. 代际与重启对账

- `intent_epoch` 持久化于 meta 表；STOP 推进代际（X01/X02 前提）。
- 网关启动时 `reconcile_inflight()`：把遗留的
  accepted/resolving/queued/executing 命令统一标 `unknown`——**不自动重试
  非幂等动作，不恢复播放意图**（X02）。
- 新会话不受旧代际影响：supersede/epoch 只作用于当时的 intent_epoch。

### 5. 来源裁决

`backfill_callback / automation / legacy` 来源提交播放类动作
（play_now/enqueue_*）→ PERMISSION_DENIED，命令记 failed（X03 的代码侧
闭环；HA 侧旧脚本停用由 cutover 检查单覆盖）。

## 后果

- T11 类重试幂等由库级唯一约束保证；迟到结果只能 superseded，不会抢播。
- v1 的确认是**收据级**：确定性单写动作（stop/volume/next）回执即确认；
  需要快照级确认的场景（音乐是否真的出声）由 IMP-08 实机验收覆盖。
- 停止结果未知时，界面如实显示「执行结果未知」，自动播放保持暂停。
