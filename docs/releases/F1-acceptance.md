# F1 服务器侧预验收（非正式）

## 验收结果：INCOMPLETE

> ⚠️ 本报告为**服务器侧预验收**，仅确认服务部署与逻辑链路正确。
> 正式 F1 验收需：真实手机 PWA 端到端、一位非开发家庭成员操作记录、
> 五项故障注入、性能样本 ≥30 次/关键动作、IMP-05 方向确认后的界面一致性
> 核对。以上条件目前均未满足，IMP-08 **重新打开**，待部署授权窗口执行。

## 环境
- XVF3800 (XIAO ESP32S3) 固件：IMP-02 版本（VAD 调试模式）
- VM1 网关：IMP-03c 控制核心 + IMP-04 auth/health
- 播放器：Squeezebox Touch (controller: 00:04:20:2)
- 测试时间：2026-09-17 深夜

## 服务器侧已通过项

| 用例 | 结果 | 证据 |
|---|---|---|
| 语音点播（播放周杰伦的晴天） | PASS | /agent 返回 play_query + ok=true，HA music_play_query 触发 |
| MA 播放器状态 | PASS | Squeezebox Touch state=playing |
| /readyz 分组件 | PASS | db ✓ ha ✓ ma ✓ stt ✓ |
| /livez | PASS | {"ok":true} |
| 补库完成不改变播放 | PASS（IMP-02 已阻断） | plays 列表为空 |

## 正式验收待完成项

- [ ] 真实手机 PWA 端到端（IMP-07 完整 UI 依赖 IMP-05 方向确认）
- [ ] 一位非开发家庭成员操作记录
- [ ] 五项故障注入（音箱离线/NAS 掉线/模型超时/补库迟到/网关重启）
- [ ] 性能样本 ≥30 次/关键动作（P95 ≤1s 控制、P95 ≤3s 出声）
- [ ] IMP-05 方向确认后的界面一致性核对
- [ ] 唤醒词启用后的正则/入口验收（IMP-12 依赖）

## 已知限制
- 唤醒词尚未启用（VAD 调试模式，房间内说话即采集）
- PWA 界面为骨架版（IMP-05 视觉方向待用户确认后完善）
- 回声消除实测未执行（IMP-12/13 范围）
- NAS 挂载核验在 Docker 容器内不可见（已加只读挂载修复，待部署验证）

## 回滚锚点
- 镜像 ID：见 version-manifest.json
- HA 包版本：2026-09-17 部署
- 回滚步骤：git revert + docker compose build + up -d
