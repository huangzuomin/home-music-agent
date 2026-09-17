# 踩坑宝典

全部来自真实运行中的故障，每一条都「踩过、定位过、修过」。按子系统分类。

## LLM / Agent

| 现象 | 根因与修复 |
| --- | --- |
| LLM 推理模型响应慢 2~3 倍且 tool_call 易截断 | DeepSeek 默认先产思考 token → 请求体带 `thinking={"type":"disabled"}` |
| Smart Path 偶发 25s+ | 并发劣化 → 硬超时（8s socket 级）< 软超时（12s），超时降级规则路径 |
| 「音量小一点」被解析成音量=1 | 中文数字「一」与「一点」混匹配 → 中文数字分支强制要求动词 |
| 绝对音量无动作，说「调到30」只掉一格 | 规则层只有相对音量 → 新增 `volume_set`（阿拉伯/中文数字/百分比/口语量词） |

## Home Assistant

| 现象 | 根因与修复 |
| --- | --- |
| refresh_token 续期永远 400 | client_id 必须与 HA 对外 URL **逐字符一致**（含端口结尾 `/`） |
| 自动化 03:30 触发实际在 11:30 | `homeassistant.time_zone` 未配 → HA 按 UTC；容器 TZ 不影响触发时区 |
| 改了 YAML 行为没变 | rest_command / script / automation / input_text **各自独立 reload** |
| 引用新 rest_command 报 Action not found | 只 reload script 不够 → 必须 `POST /api/services/rest_command/reload` |
| HA 提交补库返回 422 | Jinja 把布尔渲染成 Python 的 `True`，不是 JSON 的 `true` |
| persistent_notification 建了读不到 | 部分版本 websocket `persistent_notification/get` 恒 0 条 → 回执改用状态实体 |
| 触发时刻别看日志 | 日志是容器本地时区，`last_triggered` 是 UTC 序列化——两者不一致极易误判 |
| /root 属主文件写不了 | `docker exec -i <容器> tee <路径>` 绕过 |

## Music Assistant / 曲库

| 现象 | 根因与修复 |
| --- | --- |
| 「说有在放但没声音」 | `music/search` 跨 provider——库里没有也返回结果；判据必须是 `library://` 前缀 |
| 未登录 QQ 在线只播 ~60s | 垫场权宜之计，靠补库兜底 |
| MA 自带推荐 0 条 | 库小 + 无播放记录 + QQ 未登录 → 自建口味画像 + 榜单探索替代 |
| 「周杰伦 稻香」被判库里已有 | 只匹配到歌手名 → 加曲目覆盖率要求 |
| 挪走文件后仍报「库里已有」 | MA 索引未刷新 → **挪/删文件后必须重扫** |
| 队列里出现两首同名歌 | 决策层筛完、下载层又自己重选 → 把已筛清单经 `--approved` 传给下载层 |
| 空窗（垫场播完补库未完） | `music_play_query` 追加 2 首本地随机曲到队尾（add 不 replace） |

## musicdl / 补库

| 现象 | 根因与修复 |
| --- | --- |
| musicdl search 永久挂死 | IPv6 黑洞 + 无超时 → IPv4 优先 + 全局超时 + 守护线程硬超时 |
| ThreadPoolExecutor 超时无效 | 退出时 join 非守护线程 → 用 daemon Thread + join |
| 单曲下到 200MB+ | 聚合源 flac 无上限、音质评分反而加分 → `MAX_FILE_MB` 硬闸 |
| 繁体原唱整条漏判 | `"周杰伦" in "周杰倫"` 恒 False → zhconv 折叠 |
| 给 search 输出加一列就解析错位 | 正则刮表格不可靠 → 改 `--json` |
| `.mgg` 下载成功但只有歌词 | 酷狗加密格式 → 按扩展名硬拒收 |
| 「原唱 XXX」钓鱼标题 | 标称超长时长 → 相关度降权 −60 |

## 语音客户端 / 嵌入式

| 现象 | 根因与修复 |
| --- | --- |
| ffmpeg 强杀得到 0 字节录音 | 输出缓冲只在正常关闭时落盘 → stdin 送 `q` |
| 装了 openwakeword 仍报 No module | pip 与 run.bat 用的不是同一个 Python 解释器 |
| TTS 播一次后唤醒「死」 | `pause_wake` 有人调 `resume_wake` 没人调 → 配对 + fail-safe 超时恢复 |
| 控制台中文乱码 / 脚本崩 | GBK 控制台：别强推 utf-8，别用非 GBK 字符，见 `_console.py` |
| XMOS 完全不上线 | GPIO2 复位线被拉低/悬空 → 固件启动拉高并保持 |
| I2S 从机模式无数据 | XMOS 不出时钟 → ESP32 做 MASTER（角色与官方 yaml 标注相反） |
| 一上传就 LoadProhibited（崩在 WiFi 栈） | multipart 组包越界写坏 PSRAM 堆 → 余量给足 320 字节 |
| eloquentarduino/tflm_esp32 唤醒模型输出恒 0 | 内核对流式模型不可用 → 换 espressif 官方 esp-tflite-micro |
| 原生 USB CDC 板 RTS/DTR 复位无效 | XIAO 无 USB-UART 芯片 → 复位用 esptool 或断电 |

## 运维习惯（少踩一半坑）

1. 改代码三步：本地改真源 → 跑单测 → 同步副本 + 重建镜像，少一步就白改
2. 每类 YAML 组件独立 reload；改完即验，别攒
3. 回执/验证用「能读回的状态」，别用「写进去就不管」的通道
4. 守卫规则宁可错杀不可放过——曲库污染的清理成本远高于漏收一首歌
