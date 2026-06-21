# 架构详解

## 三层 LLM 协作

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                  │
│    用户语音 → ASR → DeepSeek V4 (虾哥后端) → 决策                  │
│                                                                  │
│    ┌─ 简单：用虾哥官方 MCP 工具 (天气/笑话/音乐/...)              │
│    │                                                             │
│    └─ 复杂：调用 claude_code(task) MCP 工具 ──┐                 │
│                                                ↓                 │
│   ┌─────────────────────────────────────────────────┐           │
│   │  你 Mac 上的 bridge:                              │           │
│   │  mcp_pipe.py (wss←→stdio)                       │           │
│   │       ↓                                          │           │
│   │  server.py (FastMCP, 4 个工具):                   │           │
│   │   - claude_code(task)            异步主要工具    │           │
│   │   - claude_code_quick(task)      ≤20s 同步       │           │
│   │   - claude_code_background(task,msg) 长任务      │           │
│   │   - claude_code_check_results()  取异步结果      │           │
│   │       ↓ spawn                                    │           │
│   │  claude --print TASK                            │           │
│   │       ↓                                          │           │
│   │  Claude Code (本地 Anthropic Claude):            │           │
│   │   - memory (你的工作记忆)                        │           │
│   │   - skills (wechat-write/wiki/ai-radar/...)      │           │
│   │   - MCP servers (outlook/teams/sap-wiki/...)     │           │
│   └─────────────────────────────────────────────────┘           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## 协议层级

| 层 | 协议 | 端点 |
|---|---|---|
| 设备 ↔ 虾哥服务器 | WebSocket / MQTT+UDP | `wss://api.tenclass.net/xiaozhi/v1/` 或 `mqtt.xiaozhi.me` |
| 虾哥 LLM ↔ 用户 MCP | WebSocket (MCP JSON-RPC) | `wss://api.xiaozhi.me/mcp/?token=JWT` |
| 用户 Mac bridge → MCP server | stdio JSON-RPC | subprocess pipe |
| MCP server → Claude Code | subprocess + stdout | `claude --print TASK` |

## 关键决策

### 为什么 claude_code 默认异步

xiaozhi.me 的 wss MCP 链接 keepalive 是 **30 秒**——MCP server 必须 30 秒内返回结果，否则虾哥服务端断开连接：

```
sent 1011 (internal error) keepalive ping timeout
```

但 `claude --print` 跑一个 memory 查询通常 60-120 秒。

**解决**：`claude_code(task)` 默认 spawn 后台 task **立刻返回**口语回执 "派蒙正在查 X，稍等一下问我"。后台 task 跑完写文件到 `/tmp/claude-bridge-outbox/{timestamp}.txt`。用户下次问"派蒙查到了吗"时 LLM 调 `claude_code_check_results` 取最新文件 + 移到 done/。

副作用：用户感知**两轮对话才能拿到结果**。但是把响应时间从"30 秒后超时报错"改成"5 秒回 + 任意时间取"，更可控。

### 为什么不用 OpenAI 兼容代理

xiaozhi.me 官方 FAQ Q29 明确不支持自定义 OpenAI 兼容 endpoint 作主对话 LLM。

也试过自部署 `xiaozhi-esp32-server` —— 但需要装 Redis + FunASR runtime（PyTorch 那一套，几 GB）+ 外接 ASR/TTS key。负担太重。

**用 MCP 工具桥接是最轻方案**：虾哥默认服务器免费 + 国产 LLM 流畅，Claude 只在需要时被动调用。

### 为什么用 stdio MCP 而不是 HTTP

mcp_pipe.py 用 stdio 转 wss 是 [虾哥官方示范](https://github.com/78/mcp-calculator)。stdio 模式好处：
- 无需开端口（不用配防火墙）
- 子进程隔离（FastMCP 实例 crash 时 mcp_pipe 自动重启）
- 与 `claude --print` 一样的 subprocess 模型，统一

## 改了上游固件什么

参考 [上游 78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 官方代码，本项目 [fork](https://github.com/heavenchenggong/StackChan-XiaoZhi/tree/codex-refactor) 改了：

### 1. m5stack-core-s3 board 加 servo MCP 工具
原版没暴露 servo 控制给 LLM（servo 自主行为：人脸追踪 + 空闲扫视 + 对话时点头）。本项目加：
- `self.head.move(yaw, pitch)` — 移到指定角度并锁定
- `self.head.center` — 回中并解锁人脸追踪
- `self.head.nod` — 当前位置点头
- `self.head.shake` — 当前位置摇头

### 2. 修复"servo 设了又弹回"
见 [engineering-notes.md §4](engineering-notes.md#4-servo-控制三重并发覆盖) 三重并发覆盖：
- FaceTracker manual lock
- Application Resume 检查 lock
- Nod/Shake 用 servo `GetCurrentYaw/Pitch` 作 base（不是 tracker 的 yaw_）

### 3. 禁用 light sleep
官方 `PowerSaveTimer(-1, 30, -1)` — 30 秒空闲进 light sleep，**关麦克风**。导致"你好小智"在 sleep 后失效，只能触摸唤醒。

改成 `PowerSaveTimer(-1, -1, -1)` 永不 sleep，麦克风永远在线。代价：CPU 略多功耗，但 stack-chan 是有线供电，不要紧。

### 4. GH Actions CI 配置
原 repo 没 build workflow，需要本地装 ESP-IDF v5.5.2（~2GB 工具链）。这里加了 [build-cores3.yaml](https://github.com/heavenchenggong/StackChan-XiaoZhi/blob/codex-refactor/.github/workflows/build-cores3.yaml)，用官方 `espressif/idf:v5.5.2` Docker 镜像 build，4 分钟产出完整 flash 包。

## bridge 工作流详解

### 启动

```
launchd 启动 mcp_pipe.py
    ↓
读 mcp_config.json 拿到 stdio server 配置
    ↓
spawn server.py 作为子进程，hold 它的 stdin/stdout
    ↓
连接 wss://api.xiaozhi.me/mcp/?token=...
    ↓
Pump:
  - 收 wss 消息 → 写 stdin
  - 读 stdout → 发 wss
```

### 工具调用

```
虾哥发 "tools/call name=claude_code arguments={task: 'X'}"
    ↓ wss
    ↓ stdio
mcp_pipe 写入 server.py stdin
    ↓
FastMCP 路由到 @mcp.tool() claude_code
    ↓
spawn asyncio.create_task(_bg())  ← 后台跑
    ↓ 立刻返回
    "派蒙正在查「X」，稍等一下再问我结果"
    ↓ stdio → mcp_pipe → wss
虾哥收到回复，把字符串 TTS 给设备
    ↓
后台 _bg() 在 60-300 秒后：
    subprocess.run(["claude", "--print", "X"])
    把 stdout 写到 /tmp/claude-bridge-outbox/{ts}.txt
```

### 取结果

```
用户："派蒙查到了吗" → DeepSeek → tools/call claude_code_check_results
    ↓
取 outbox 里最新 .txt → 移到 done/ → 返回内容（自动截 600 字）
    ↓
虾哥 TTS 念给用户
```

## 性能数字

| 阶段 | 实测延迟 |
|---|---|
| ASR (虾哥流式) | 100-500ms |
| DeepSeek V4 决策 + 调工具 | 500ms-1s |
| wss MCP 往返（虾哥 ↔ 你 Mac） | 50-200ms |
| 立刻回执 TTS | 500ms-1s |
| **用户感知"派蒙正在查"出现** | **2-4 秒** |
| 后台 claude --print 跑 memory 查询 | 30-120 秒 |
| 后台 claude --print 跑 skill (写公众号) | 60-300 秒 |
| 用户问"查到了吗" → 念结果 | 同上 2-4 秒 |
