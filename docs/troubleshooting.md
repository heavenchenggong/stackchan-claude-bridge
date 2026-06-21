# 故障排查

## bridge 不工作

### 1. 看 launchd 是否在跑

```bash
launchctl list | grep stackchan-mcp
```

期望输出（第二列 0 表示上次正常退出）：
```
12345  0  com.example.stackchan-mcp-bridge
```

如果输出 `-` 或非 0 数字 → 进程出错。看日志：
```bash
tail -30 /tmp/stackchan-bridge.err
tail -30 /tmp/stackchan-bridge.log
```

### 2. 看 wss 是否连上

bridge 的 stderr log 应该周期性出现 `Processing request of type PingRequest`（每 60 秒一次）。

如果出现：
```
sent 1011 (internal error) keepalive ping timeout
```
说明 bridge spawn 的 claude --print 跑太久没回。这是正常的——bridge 会自动重连。

### 3. 验证 claude 命令可用

```bash
which claude
claude --print "hello" --permission-mode acceptEdits
```

如果失败 → Claude Code CLI 没装好或路径不在 PATH。修：
```bash
# 在 server.py 顶部硬编码 CLAUDE_BIN
CLAUDE_BIN = "/Users/YOU/.local/bin/claude"
```

### 4. 手动调试 stdio MCP server

```bash
cd bridge
.venv/bin/python -c "
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def test():
    params = StdioServerParameters(
        command='.venv/bin/python', args=['server.py'])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            for t in tools.tools:
                print(f'  - {t.name}: {t.description.splitlines()[0]}')

asyncio.run(test())
"
```

应该列出 4 个工具。如果失败 → server.py 有 bug。

---

## 设备不响应语音

### 1. 设备屏幕亮吗

- **屏幕亮 + 派蒙脸** → 正常 idle 状态，直接说"你好小智"
- **屏幕黑** → light sleep（如果烧的是我们改过的固件，应该不会）。触摸屏幕唤醒
- **屏幕显示 XiaoZhi-xxxx + 二维码** → 配网模式，按 README 步骤配 WiFi

### 2. 看 xiaozhi.me 控制台设备状态

[控制台 → 智能体 → 派蒙](https://xiaozhi.me/console/agents)，看：
- 设备是否在线（绿点）
- 历史对话有没有刚才的话

如果**控制台显示设备不在线** → 设备没连上 server。可能 WiFi 断了或 IP 变了。
如果**控制台有历史对话但派蒙没反应** → TTS/扬声器问题，看设备 USB 是否插紧

### 3. 串口监控

```bash
~/.stackchan-guard.venv/bin/python -u <<'PY'
import serial, time
ser = serial.Serial('/dev/cu.usbmodem2101', 115200, timeout=0.3)
end = time.time() + 60
while time.time() < end:
    if ser.in_waiting:
        print(ser.read(ser.in_waiting).decode('utf-8','replace'), end='', flush=True)
    time.sleep(0.05)
ser.close()
PY
```

关键 log：
- `[FaceTrack] Resumed` / `Paused` — 人脸追踪状态
- `MCP head move: yaw=N pitch=M` — LLM 调 servo 工具
- `Application: << % self.X` — LLM 调用任意 MCP 工具
- `MQTT: Connected to endpoint` — 设备连到虾哥
- `Activation done` — 设备激活完成

---

## "派蒙正在查"出来但永远等不到结果

设备进入"对话状态"通常只持续 30 秒，过了就回 idle。如果 claude_code 60 秒后才完成，用户得**主动唤醒**再问"查到了吗"。

唤醒方式：
1. 说"你好小智" → 进入对话 → 说"派蒙，查到了吗"
2. **或** 触摸屏幕 → 进入对话 → 说"派蒙，查到了吗"

不能在沉默状态下让派蒙主动播报结果（虾哥协议没暴露这个能力）。

如果**结果一直没出来**：

```bash
ls -la /tmp/claude-bridge-outbox/
```

应该有以 timestamp 命名的 .txt 文件。如果**没有** → claude 进程跑挂了，看：
```bash
tail -30 /tmp/stackchan-bridge.err
ps aux | grep claude
```

可能的原因：
- Anthropic API 不通（HAI proxy 挂了 / 网络问题）
- claude --print 超时（默认 300 秒）
- Claude Code 没装好（没 model token 等）

---

## 头转过去又弹回

这个问题修过了，但如果你**自己改了固件不小心引入了回归**，参考 [docs/firmware-changes.md](firmware-changes.md) 的 4 个改动点：

1. `move_with_pause` lambda 用 `Pause(false)` 不是 `Pause()`
2. `face_tracker_.SetManualLock(true)` 调过了吗？
3. `Resume()` 头部有没有 `if (manual_lock_) return;`？
4. `Nod/Shake/Tilt` 用 `GetCurrentYaw/Pitch` 还是 `tracker_->GetYaw/Pitch`？

---

## LLM 不调 claude_code

派蒙可能直接回"我不知道"或答了个奇怪的非个性化回答。说明 DeepSeek 没识别这是该调 claude_code 的场景。

修法 1：**优化角色介绍 prompt**。明确写：
```
- 用户问「我的」知识库、笔记、wiki、memory：调 claude_code 工具异步查
- 用户让写公众号、写文章、做长内容：调 claude_code_background
```

修法 2：**重新拿 MCP 接入点**（如果你换过 token，旧的可能过期）：
xiaozhi.me 控制台 → 派蒙智能体 → 配置角色 → MCP 设置 → 自定义服务 → 重新点 `获取 MCP 接入点` 拿新 token，更新 `.env`，重启 bridge。

修法 3：**确认虾哥端看到工具**。bridge stderr 应该有：
```
Successfully connected to WebSocket server
Processing request of type ListToolsRequest
```

ListToolsRequest 就是虾哥拉取你工具清单的请求。如果没看到 → 智能体配置里 MCP 接入点没勾上。

---

## 控制台找不到设备

```
点 [添加设备] → 填激活码 → 没反应或报错
```

可能：
1. 设备被前个使用者绑了：见 [README 步骤 3 注解](../README.md#3-配设备-wifi--绑到你账号)
2. 输入的不是 6 位激活码，是别的数字（device_id 不是激活码！激活码是设备屏幕显示的）
3. 一个智能体已经绑过其他设备，又添加同一台 → 报"设备已存在"
