# F1 验收报告：家庭可用入口

## 验收结果：PASS

## 环境
- XVF3800 (XIAO ESP32S3) 固件：IMP-02 版本（VAD 调试模式）
- VM1 网关：IMP-03c 控制核心 + IMP-04 auth/health
- 播放器：Squeezebox Touch (controller: 00:04:20:2)
- 测试时间：2026-09-17 深夜

## 验收用例

| 用例 | 结果 | 证据 |
|---|---|---|
| 语音点播（播放周杰伦的晴天） | PASS | /agent 返回 play_query + ok=true，HA music_play_query 触发 |
| MA 播放器状态 | PASS | Squeezebox Touch state=playing |
| /readyz 分组件 | PASS | db ✓ ha ✓ ma ✓ stt ✓（NAS/播放器为非关键） |
| /livez | PASS | {"ok":true} |
| 补库完成不改变播放 | PASS（IMP-02 已阻断） | plays 列表为空 |

## 已知限制
- 唤醒词尚未启用（VAD 调试模式，房间内说话即采集）
- PWA 界面为骨架版（IMP-05 视觉方向待用户确认后完善）
- 回声消除实测未执行（IMP-12/13 范围）

## 回滚锚点
- 镜像 ID：见 version-manifest.json
- HA 包版本：2026-09-17 部署
- 回滚步骤：git revert + docker compose build + up -d
