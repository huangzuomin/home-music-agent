# Windows 语音客户端

薄客户端：只做「采音 → 唤醒/断句 → 上传 → 显示」，所有业务逻辑留在服务端。
这保证了服务端契约不变时，客户端可以换成树莓派、嵌入式固件等任何形态。

## 链路

```
【常驻唤醒模式（主用法）】
麦克风 ─▶ AudioCapture（常驻，16k 单声道 PCM）
          ├─ WakeWord（openWakeWord，本地检测，不上传）
          ├─ RingBuffer 1200ms（防吞句首）
          ├─ VAD（能量 + 自适应噪声底，自动断句）
          └─ 一段完整语音
               ▼
         VM1 STT :8100 → 文本 → /agent :8200 → HA → MA → 音响
               ▼
         TTS 播报（播报期间屏蔽唤醒，防自唤醒）

【按住说话（Debug / Fallback）】
按住按钮或 F9 → ffmpeg 录音 → STT → /command（规则网关）
```

## 安装

- Windows 10+，Python 3.9+
- ffmpeg（设置 `FFMPEG` 环境变量或放入 PATH）
- `pip install requests openwakeword`（首次运行 `download_wakeword_models.py` 下模型）

> ⚠️ **解释器陷阱**：Windows 上常有多个 Python 共存（托管环境 vs 系统安装）。
> `pip install` 用的解释器必须与 `run.bat` 实际调用的解释器是同一个，否则
> 常驻模式会报 `No module named 'openwakeword'`。

## 配置

复制 `config.example.json` 为 `config.json`，填入服务端 IP：

| 键 | 说明 |
| --- | --- |
| `stt_url` / `agent_url` / `tts_url` | 服务端三个端点 |
| `session_id` | 会话 id，同一段听音乐的上下文靠它串联 |
| `device` | 录音设备名（`run.bat --list-devices` 可枚举） |
| `voice.wake_word` / `wake_threshold` | 唤醒词模型与阈值 |
| `voice.vad.*` | 断句参数（起判阈值、静音判定、最长语句） |

## 运行与自检

```powershell
.\run.bat --voice          # 常驻唤醒模式（主用法）
.\run.bat                  # 图形界面（按住说话）
.\run.bat --mic-level 8    # 只测麦克风电平（验收前自检）
.\run.bat --say "播放周杰伦的晴天"   # 跳过语音直测 Agent
.\run.bat self_test_voice.py   # 四段自测（RingBuffer/VAD/采集/状态机）
.\run.bat test_wakeword_audio.py   # 端到端验证唤醒词（TTS 合成音频喂模型）
```

实测参考值（DJI Mic 蓝牙 HFP 通道）：环境噪声 ≈ -61 dBFS，正常说话 ≈
-32 dBFS，唤醒词峰值 ≈ 0.999（阈值 0.6）。电平低于 -60 dBFS 说明麦克风
没开或没配对。

## 实现约束（对照实验换来的，改代码前必读）

1. **ffmpeg 收尾必须 stdin 送 `q`**，`terminate()` 强杀得到 0 字节——输出
   缓冲只在正常关闭时落盘。推论：不能加 `-nostdin`，stdin 必须是管道。
2. **ffmpeg 打开 DirectShow 设备要 0.4~0.6s**，预热期说话会丢。用 stderr 的
   `Press [q] to stop` 作为「设备已就绪」信号（`-loglevel info` 才打印，
   常驻采集不要用 warning 级别）。
3. **常驻采集只有一个 ffmpeg 实例**，唤醒/VAD/录音共用同一帧流；多开会互抢。
4. **处理一轮对话期间必须停唤醒检测**（STT+Agent+TTS 要 3~5s，否则把 TTS
   当成人声）。实现是 `_busy` 闸门 + `pause_wake()/resume_wake()`——**必须
   配对**，且要有 fail-safe 超时强制恢复（`pause_wake` 有人调、`resume_wake`
   没人调 = 引擎永久失聪，此坑踩过）。
5. **控制台编码**：中文 Windows 控制台是 GBK（CP936）。不要 `reconfigure`
   成 utf-8（中文乱码），也不要用 ✅❌ 等非 GBK 字符（UnicodeEncodeError）。
   正确做法见 `_console.py`：沿用控制台编码、只把错误处理放宽为 replace。
