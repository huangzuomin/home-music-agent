# A5 生产收口待办清单（下次授权窗口执行，本轮不动现场）

> 所有项均已完成**代码与配置变更**，仅剩**部署切换**（在授权窗口一次性执行）。
> 切换步骤按 imp02-cutover-checklist.md 执行，本清单是其安全增强延伸。

## 1. 端口收口（五端口改绑 127.0.0.1）

- [ ] compose.yaml：stt-service ports 改为 "127.0.0.1:8100:8100"
- [ ] compose.yaml：voice-gateway 已 network_mode: host（无法单独收口）
      → 方案：voice-gateway 改为 bridge 网络 + 映射 127.0.0.1:8200:8200
      （需同步修改 caddy 路由和 HA_BASE 为 http://127.0.0.1:8200 不变）
- [ ] music-fetcher 同理改 bridge + 127.0.0.1:8300:8300
- [ ] music-assistant MA 端口 8095 改绑 127.0.0.1（仅 caddy 需要访问）
- [ ] home-assistant 8123 保留 LAN 可达（家庭成员浏览器访问）
- [ ] 验证：caddy HTTPS 443 入口正常代理 /gw/* /stt/* 和 PWA 静态页

## 2. DEVICE_AUTH_REQUIRED=true

- [ ] voice-gateway 环境变量 DEVICE_AUTH_REQUIRED=true（compose 或 .env）
- [ ] 先生成配对码，让 PWA 和 Windows 客户端配对获取令牌
- [ ] 验证：无令牌请求 → 401；有令牌 → 正常
- [ ] Windows 客户端 config.json 增加 device_id + device_token
- [ ] XVF3800 固件如需上传 → 同步增加设备令牌头

## 3. NAS 掉线防护（X08 兜底）

- [ ] compose：music-fetcher 加 healthcheck（检查 /mnt/music 可写）
- [ ] readiness nas_mount=false 时 fetcher 自动暂停新任务
- [ ] 长期：考虑改用 Docker named volume + NFS CSI driver

## 4. 部署目录版本管理（R-NEW-01）

- [ ] /opt/home-music-agent 初始化 git 仓库（或用 rsync 校验和清单）
- [ ] 首次提交当前基线（排除 .env / data/ / backups/）

## 5. 回滚锚点（每次部署后更新）

- [ ] 记录 voice-gateway / music-fetcher 镜像 ID
- [ ] 记录 HA 包版本（home_music_agent.yaml md5）
- [ ] 记录 PWA www 文件 md5
