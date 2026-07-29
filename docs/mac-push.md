# Mac 主动推送：speak / express / gesture

让 **Mac 上的 agent 主动**（不需要人先开口）让 Stack-chan 说一段任意文本、换表情、点头/摇头。

这是现有 bridge 的**反方向**能力：

| | 方向 | 触发 | 传输 |
|---|---|---|---|
| 现有 bridge (`server.py` + `mcp_pipe.py`) | 云 → Mac（**PULL**） | 人说话 → 小智 LLM `tools/call` | `wss://api.xiaozhi.me` |
| 本能力 (`stackchan_push.py`) | **Mac → 设备（PUSH）** | Mac 上任何脚本/agent 自己发起 | 局域网 `ws://<device>:8080/ws` |

设计依据：scout 报告 Path A2（`speak` 的音频在 **Mac** 上用 macOS `say` 合成——设备本身没有 TTS，只会拉 URL 播放 Opus）。**全程不经过云，设备空闲也能说话。**

---

## 前置条件

- **设备侧固件**：需刷入 LAN 控制服务器 + `self.play_audio_url` 工具（scout 报告 Path A2，与本 PR **并行开发**）。固件未刷时，`speak` 会合成音频、起服务、发出 MCP 调用，但设备不会来拉 → 打印清晰告警（不会崩）。`express` / `gesture` 用的 `self.face.expression` / `self.head.nod` / `self.head.shake` 是**现有固件工具**（见 [firmware-changes.md](firmware-changes.md)），只要 LAN 服务器就位即可用。
- **Mac 侧工具**：`ffmpeg`（带 `libopus` 编码器）+ `say`（macOS 自带）。`python stackchan_push.py doctor` 一键自检。
- **同一局域网**：设备要能反向连到 Mac 的 HTTP 文件服务（家里同一个 WiFi 即可）。

---

## 接口契约（必须与固件一致）

```
LAN 端点  : ws://<device-ip>:8080/ws
消息信封  : {"type":"mcp","payload":{"jsonrpc":"2.0","id":<n>,
              "method":"tools/call","params":{"name":"<tool>","arguments":{...}}}}
speak     : name=self.play_audio_url  arguments={"url":"http://<mac-ip>:<port>/<uniq>.ogg","token":"<shared>"}
express   : name=self.face.expression arguments={"emotion":"<face>"}
gesture   : name=self.head.nod / self.head.shake
认证      : 每个请求都在 arguments 里带 token
```

**两个需要和固件对齐的假设**（定稿后各是一行改动）：

1. **token 放哪**：契约里唯一带 token 的例子是 `play_audio_url` 的 `arguments`。为满足"每个请求都带 token"，本实现把 token 注入**每个** tool 的 `arguments`。若固件改为在信封层校验，改 `build_envelope()` 一行即可。
2. **音频格式**：由固件 Opus 解码器决定采样率/帧长。固件 PR 定稿前，默认用参考实现验证过的值 **16kHz 单声道 / 60ms Opus 帧 / OGG 容器**，并做成 env 可配（`SC_OPUS_RATE` / `SC_OPUS_FRAME_MS`），对齐固件只需改环境变量。
   > 注：`ffprobe` 会把 OGG 头里的采样率显示成 48000 Hz——这是 Ogg-Opus 的标准行为（源已重采样到 `SC_OPUS_RATE`，解码端按需重采样），不是 bug。

---

## 配置（env / `bridge/.env`，不硬编码密钥）

| 变量 | 必须 | 默认 | 说明 |
|---|---|---|---|
| `SC_DEVICE_IP` | ✅ | — | 设备局域网 IP（建议路由器 DHCP 保留 / mDNS） |
| `SC_TOKEN` | ✅ | — | 与固件共享的认证 token |
| `SC_DEVICE_PORT` | | `8080` | 设备 LAN WS 端口 |
| `SC_DEVICE_WS_PATH` | | `/ws` | 设备 LAN WS 路径 |
| `SC_MAC_IP` | | 自动 | Mac 局域网 IP（写进音频 URL）。**自动探测优先选与设备同网段的网卡，避开 VPN/utun**；开 VPN 时建议显式写死 |
| `SC_SERVE_PORT` | | `8790` | Mac 临时 HTTP 文件服务端口 |
| `SC_SERVE_DIR` | | `/tmp/sc` | 存放待播 `.ogg` 的目录 |
| `SC_OPUS_RATE` | | `16000` | Opus 采样率（对齐固件） |
| `SC_OPUS_FRAME_MS` | | `60` | Opus 帧长 ms（对齐固件） |
| `SC_TTS_VOICE` | | 系统音色 | `say` 音色，中文可用 `Meijia` / `Tingting` |

---

## 用法（CLI）

```bash
cd bridge
# 自检：打印解析后的配置 + 检查 say / ffmpeg(libopus)
./.venv/bin/python stackchan_push.py doctor

# 让机器人说话（可同时换表情 + 点头）
./.venv/bin/python stackchan_push.py speak "跑完了，HYPE 回测 Sharpe 1.8" --face happy --gesture nod

# 只换表情 / 只点头
./.venv/bin/python stackchan_push.py express happy
./.venv/bin/python stackchan_push.py gesture nod

# 不碰设备，只把要发的 JSON 打出来（调试 / 没设备时）
./.venv/bin/python stackchan_push.py speak "测试" --dry-run
```

也可作为库调用（供 job 完成回调 / hook / cron 使用）：

```python
import asyncio
from stackchan_push import Config, speak
asyncio.run(speak(Config.from_env(), "跑完了，Sharpe 1.8", face="happy", gesture_kind="nod"))
```

### `speak` 内部流程

```
speak(text)
  1. say -o utt-<uniq>.aiff "text"                      # Mac 本地 TTS
  2. ffmpeg -c:a libopus -ar 16000 -ac 1 -frame_duration 60 -f ogg  → utt-<uniq>.ogg
  3. 在 SC_MAC_IP:SC_SERVE_PORT 起一个临时 HTTP 服务（只服务该 .ogg）
  4. ws://<device>:8080/ws 发一条 MCP: self.play_audio_url {url, token}
  5. 等设备来 GET 那个 URL（确认拉到音频）→ 短暂 grace 后关闭 HTTP 服务
```

- **唯一文件名**（`utt-<毫秒>-<随机>.ogg`）避免设备播放到旧缓存。
- 设备拉不到（固件没刷 / 网络不通）→ 打印清晰告警，不阻塞。
- 连不上设备 / token 错 → 抛清晰错误（`DeviceUnreachable` / `DeviceError`）。

---

## 从 PULL 升级到 PUSH（bonus，本 PR 不动现有流程）

现有"报告给我"是 **PULL**：`claude_code` 把结果写到 `/tmp/claude-bridge-outbox/`，等人问"派蒙查到了吗"才念（见 [architecture.md](architecture.md)）。

有了 `speak`，完成回调可以直接 **PUSH**——跑完就让机器人自己念：

```python
# 在 server.py 的后台任务 _bg() 收尾处（示意，本 PR 未改动 outbox 流程）：
from stackchan_push import Config, speak
await speak(Config.from_env(), f"{task[:20]} 跑完了")
```

本 PR **不删除** outbox 流程，只是让它成为可选：两种都留着。

---

## 本地验证（无实体设备）

```bash
cd bridge
./.venv/bin/ruff check stackchan_push.py tests/test_stackchan_push.py
./.venv/bin/python tests/test_stackchan_push.py     # mock 设备 E2E，9 项
```

`tests/test_stackchan_push.py` 起一个 **mock 设备**（asyncio WS 服务，校验 token，并像固件一样**真的去 HTTP GET** 音频 URL），端到端验证：信封契约、token 注入、`say`+`ffmpeg` 产出有效 OGG、`express`/`gesture` 往返、bad-token→错误、设备不可达→错误、`speak` 全链路（合成→服务→发送→设备拉取→+表情+点头）、LAN IP 同网段优选（避开 VPN）。
