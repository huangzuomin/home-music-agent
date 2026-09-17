# 服务端部署

## 前置条件

- Ubuntu 20.04+（物理机或 VM，2C4G 起步）+ Docker Compose
- 音乐目录：NAS（NFS/SMB 挂载到 `/mnt/music`）或本机目录，MA 容器内为 `/music`
- Home Assistant 与 Music Assistant 的管理员账号（首次启动后初始化）
- 一个 OpenAI 兼容协议的 LLM API Key（默认适配 DeepSeek，智谱 GLM 亦可）

## 部署步骤

```bash
git clone https://github.com/huangzuomin/home-music-agent.git
cd home-music-agent
cp .env.example .env && vi .env     # 填写凭据与 IP
docker compose up -d --build
```

### 时间区（重要）

`compose.yaml` 中各服务已设 `TZ=Asia/Shanghai`，但 **HA 的自动化触发时区由
`configuration.yaml` 的 `homeassistant.time_zone` 决定**，与容器 TZ 无关。
不配置的话 `at: "03:30:00"` 会按 UTC 执行（= 北京时间 11:30）。

```yaml
# config/home-assistant/configuration.yaml
homeassistant:
  time_zone: Asia/Shanghai
```

改完必须 `docker restart home-assistant`（该键不能 reload）。

### 音乐目录

compose.yaml 默认把宿主机 `/mnt/music` 挂为 MA 与 music-fetcher 容器内的
`/music`（读写）。NFS 挂载示例（/etc/fstab）：

```
192.168.1.10:/volume4/music  /mnt/music  nfs  rw  0  0
```

## Home Assistant 编排包

把 `services/home-assistant/packages/home_music_agent.yaml` 复制到 HA 的
`config/packages/`，然后在开发者工具里依次重载：

- `script`、`automation`、`rest_command`、`input_text` —— **各自独立 reload，
  只 reload automation 不会刷新 rest_command 的 url/timeout**

新增/修改 `rest_command:` 后必须 `POST /api/services/rest_command/reload`，
否则 script 里引用新 rest_command 会报 "Action not found"。

## 长效令牌

HA 与 MA 之间用长效令牌（LLAT）认证，避免 refresh_token 续期 401 的坑。
`services/music-fetcher/ha_mint_token.py` 可用 refresh_token 铸造 3650 天
令牌并写回 .env。注意：**续期的 client_id 必须与 HA 对外 URL 逐字符一致**
（含端口与结尾 `/`），不一致就永远 400。

## 凭据说明

| 变量 | 用途 |
| --- | --- |
| `MA_LONG_TOKEN` | music-fetcher / voice-gateway 调 MA（查库、重扫） |
| `HA_ACCESS_TOKEN` / `HA_REFRESH_TOKEN` / `HA_CLIENT_ID` | music-fetcher 回调 HA 播放 |
| `LLM_API_KEY` / `LLM_BASE` / `LLM_MODEL` | voice-gateway 的 Smart Path |

## 运维速查

```bash
docker compose ps                                   # 服务状态
curl -s http://127.0.0.1:8300/health                # 补库服务自检
docker logs voice-gateway --since 30m               # 网关日志
curl -s http://127.0.0.1:8300/jobs | python3 -m json.tool   # 最近补库作业
# 曲库（宿主机路径是 /mnt/music，容器内才是 /music）
ls /mnt/music/
```

## 已知坑（部署阶段）

- Dockerfile 的 COPY 是**白名单**——新增 .py 必须同步加进对应 Dockerfile
  的 COPY 行，否则 build 后容器里没有该文件
- 改了 compose.yaml / outputs 但容器行为没变 = 没重建镜像 / 没同步副本
- `.env` 不导出（`set -a && . .env && set +a`）就手工跑脚本会静默失败
- HA `persistent_notification` 在部分版本上「建了读不到」——回执不要依赖它，
  改用状态实体（`input_text`）
