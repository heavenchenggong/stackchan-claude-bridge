# Stack-chan × Claude Code 桥接

把 [Stack-chan 桌面机器人](https://protopedia.net/prototype/2345)（M5Stack CoreS3 + 双舵机 + 摄像头）变成 **能调你本地 Claude Code 的 AI 桌面伙伴**。

- 🎤 跟 Stack-chan 说话用的是流畅的 [小智 AI 协议](https://github.com/78/xiaozhi-esp32)（DeepSeek/Qwen/豆包 后端，~1 秒响应）
- 🤖 关键时刻调本地 **Claude Code**，访问你的 memory / skills / 30+ MCP 服务（个人知识库、公众号写作、邮件、wiki 等）
- 🎯 LLM 还能控制头部角度 / 表情 / LED 灯环，所有动作语音可控

> "派蒙，查我的知识库里关于 HK 薪酬的笔记" → DeepSeek 看到 `claude_code` 工具 → wss 调到你 Mac → spawn `claude --print` → 用 personal-wiki skill 查 → 念结果给你

---

## 这是什么

Stack-chan 是日本网友设计的开源桌面机器人，[M5Stack 卖整套套件](https://shop.m5stack.com/products/m5stack-cores3-stackchan-ai-desktop-robot-kit)。配合 [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)（"小智 AI"）固件可以语音对话。

但**官方固件**：
- ❌ 不能换 Claude/GPT 作主模型（官方 server 只支持国产 LLM）
- ❌ 没暴露 servo 角度控制给 LLM
- ❌ 休眠后唤醒词失效
- ❌ 不能查你的个人知识库

**这个项目**：
- ✅ 通过 xiaozhi.me **MCP 接入点**给 LLM 加了一个 `claude_code(task)` 工具
- ✅ 工具通过 wss 路由到你 Mac 上的本地 server
- ✅ 本地 server spawn `claude --print` 跑你的 Claude Code（带 memory + skills + MCP）
- ✅ 异步模式：LLM 立刻回"派蒙正在查"，结果出来后用户说"查到了吗"再念
- ✅ 同时 fork 修了官方固件 3 个 bug（servo MCP 缺失、休眠 mic 关闭、Nod 用错 base）

整体架构：

```
你说「派蒙，查我的知识库里关于 X 的笔记」
                ↓
        Stack-chan 麦克风 ── 流式 ASR
                ↓
        虾哥后端 api.xiaozhi.me + DeepSeek V4
                ↓ 看到 claude_code 工具就 tools/call
        wss://api.xiaozhi.me/mcp/?token=...
                ↓
        你 Mac launchd 守护的 mcp_pipe.py（wss ↔ stdio）
                ↓
        bridge/server.py (FastMCP, 4 个工具)
                ↓ subprocess
        claude --print "task"
                ↓
        memory + skills + 30+ MCP 服务
                ↓ 写到 /tmp/claude-bridge-outbox/{ts}.txt
        立刻返回 "派蒙正在查 X，稍等一下问我"
                ↓
        TTS → 派蒙说话
        ......稍后......
你说「派蒙，查到了吗」
                ↓
        claude_code_check_results 取最新结果
                ↓
        TTS 念结果（自动截到 600 字）
```

---

## 适合谁？

- 已经买了 **M5Stack CoreS3 + Stack-chan 套件**（或正在考虑）
- Mac 上装了 **Claude Code CLI**（`claude` 命令可用）
- 想让 Stack-chan 对接自己的本地知识库 / 工具，而不只是"小智 AI 默认人格"

**不需要**：
- 自己装 ESP-IDF（fork 用 GitHub Actions 自动 build，直接下 release）
- 自己部署 xiaozhi-server（用虾哥免费云后端就行）
- 改 Mac 系统设置 / 装 docker / 装 Redis 等重型工具（只装一个 ~65MB 的 Python venv）

**有要求**：
- Claude Code 能跑（即接入了任意 Anthropic 兼容 LLM，或 SAP HAI proxy 之类）

---

## 快速上手

### 1. 注册小智账号 + 创建智能体

1. 打开 [https://xiaozhi.me](https://xiaozhi.me)，手机号注册
2. 登录后进 **控制台 → 智能体 → +添加**，创建一个智能体（比如叫"派蒙"）
3. 进入智能体配置：
   - **语言模型**：选 `DeepSeek V4（性格丰富）` —— 工具调用稳，性格丰满
   - **角色介绍**：见下面 [推荐角色介绍](#推荐角色介绍-prompt) 抄一份
   - **MCP 设置 → 自定义服务**：点 `[获取 MCP 接入点]`，复制弹出的 `wss://api.xiaozhi.me/mcp/?token=...` URL（这是关键，整个桥就靠这条 URL 工作）
   - 保存

### 2. 烧固件到 Stack-chan

我们的固件：[heavenchenggong/stackchan-xiaozhi-firmware](https://github.com/heavenchenggong/stackchan-xiaozhi-firmware)

**改动**：
- 加 4 个 servo MCP 工具：`self.head.move / center / nod / shake`（官方固件无）
- 禁用 light sleep（默认 30 秒后关麦克风，唤醒词失效；这里改成永远在线）
- 修 Nod/Shake 用 tracker yaw 作 base 导致 LLM 设的位置被甩回的 bug

**烧录方式**：

去 [Actions 页面](https://github.com/heavenchenggong/stackchan-xiaozhi-firmware/actions/workflows/build-cores3.yaml) 下载最新 build 产物（artifact 里有 `xiaozhi.bin` + 完整 merged-binary 等）。

完整烧录（含 NVS 抹除，第一次烧推荐）：
```bash
pip install esptool
esptool.py --chip esp32s3 -p /dev/cu.usbmodem2101 erase_flash
esptool.py --chip esp32s3 -p /dev/cu.usbmodem2101 -b 460800 \
    --before default-reset --after hard-reset \
    write-flash --flash-mode dio --flash-size 16MB --flash-freq 80m \
    0x0 merged-binary.bin
```

或者**只升级 app**（保留 NVS WiFi 凭据）：
```bash
esptool.py --chip esp32s3 -p /dev/cu.usbmodem2101 -b 460800 \
    write-flash 0x410000 xiaozhi.bin
```

### 3. 配设备 WiFi + 绑到你账号

1. 设备首次开机进 **配网模式**，屏幕显示 `XiaoZhi-xxxx` 热点 + 二维码
2. 手机/电脑连这个热点 → 浏览器自动跳出配网页（或访问 http://192.168.4.1）
3. 选你家 WiFi，输密码
4. 设备连上后会：
   - 屏幕显示 **6 位激活码** + 语音播报
   - 把激活码填到 [xiaozhi.me 控制台 → 智能体 → 添加设备](https://xiaozhi.me/console/agents)
   - 完成绑定

> **如果屏幕没显示激活码、设备直接进入对话状态** —— 说明这台设备的 MAC 已经被前一个使用者（或者作者测试时）绑过。需要语音问设备「你的设备 ID 是多少」拿到一串纯数字 device_id，然后发邮件给 `xiaozhi.ai@tenclass.com`，标题 `【解绑设备，设备ID xxx，MAC地址 xx:xx:xx:xx:xx:xx】`，正文说明情况。客服通常 1-3 工作日处理。

### 4. 部署 Mac 上的 bridge

```bash
# clone 本项目
git clone https://github.com/heavenchenggong/stackchan-claude-bridge.git
cd stackchan-claude-bridge/bridge

# 装 Python 依赖（venv 隔离，~65MB）
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 配置 wss endpoint
cp .env.example .env
# 用编辑器把 MCP_ENDPOINT 改成你的 wss URL（步骤 1 复制的那个）
vim .env

# 配置 MCP server 路径
cp mcp_config.json.example mcp_config.json
# 把 /PATH/TO/ 替换成真实绝对路径
sed -i '' "s|/PATH/TO/stackchan-claude-bridge|$(cd ..; pwd)|g" mcp_config.json

# 手动跑一下验证
python mcp_pipe.py
# 应该看到：
#   "[claude_code] Connecting to WebSocket server..."
#   "[claude_code] Successfully connected to WebSocket server"
#   "claude-bridge MCP server starting"
#   "Processing request of type ListToolsRequest"
# 看到 ListToolsRequest 说明虾哥已经发现了你的工具
# Ctrl+C 退出
```

### 5. 装 launchd 守护（让 bridge 7×24 在线）

```bash
cd ../launchd
cp com.example.stackchan-mcp-bridge.plist ~/Library/LaunchAgents/

# 编辑 plist 把 /PATH/TO/ 改成实际路径
sed -i '' "s|/PATH/TO/stackchan-claude-bridge|$(cd ..; pwd)|g" \
    ~/Library/LaunchAgents/com.example.stackchan-mcp-bridge.plist

# 加载守护
launchctl load -w ~/Library/LaunchAgents/com.example.stackchan-mcp-bridge.plist

# 验证
launchctl list | grep stackchan-mcp
tail -f /tmp/stackchan-bridge.err
```

### 6. 开始用

跟 Stack-chan 说话试试：

| 你说 | 派蒙做啥 |
|---|---|
| 你好小智 | 唤醒（休眠后也能唤醒，无需触摸屏幕）|
| 派蒙，往左转 30 度 | 头转到 yaw=-30 并稳住，不会被人脸追踪甩回 |
| 派蒙，点个头 | 在当前角度就地点头 |
| 派蒙，回正 | 回中 + 人脸追踪恢复 |
| 派蒙，亮个红灯 | LED 灯环变红 |
| 派蒙，开心一点 | 表情切到 happy |
| **派蒙，查我的知识库里有 X 吗** | 调 `claude_code` → 异步跑 Claude Code → 立刻回"派蒙正在查" |
| **派蒙，查到了吗** | 调 `claude_code_check_results` → 念结果 |
| **派蒙，帮我写一篇公众号文章关于 X** | 调 `claude_code_background` → 后台跑（可能几分钟） |

---

## Mac 主动推送：让机器人自己开口（进阶）

上面是**云 → Mac**的 PULL（人先说话，小智 LLM 调工具）。还有一个**反方向**的能力：`bridge/stackchan_push.py` 让 **Mac 上任何 agent/脚本主动**（不用人先开口）让 Stack-chan 说任意文本、换表情、点头——走**局域网**直连设备，**不经过云，设备空闲也能说**。

```bash
cd bridge
./.venv/bin/python stackchan_push.py doctor                 # 自检 say / ffmpeg / 配置
./.venv/bin/python stackchan_push.py speak "跑完了，Sharpe 1.8" --face happy --gesture nod
```

典型用法：某个后台 job 跑完，完成回调直接 `speak("跑完了…")`，机器人自己念出来——把现有"写 outbox 等人问『查到了吗』"的 PULL 流程升级成 PUSH。

- 音频在 Mac 上用 `say` 合成 → `ffmpeg` 转 16kHz 单声道 60ms Opus/OGG → 起临时 HTTP 服务 → 发一条 MCP 让设备 `self.play_audio_url` 拉取播放。
- 需设备刷入 LAN 控制固件（scout 报告 Path A2，**与本能力并行开发**）；固件未就位时 `speak` 会清晰告警但不崩。
- 配置（`SC_DEVICE_IP` / `SC_TOKEN` / `SC_MAC_IP` …）走 `bridge/.env`，见 `.env.example`。

📖 **详细契约、配置、验证见 [docs/mac-push.md](docs/mac-push.md)。**

---

## 推荐角色介绍 (Prompt)

xiaozhi.me 控制台 → 智能体配置 → 角色介绍 里贴这段（替换"派蒙"为你想要的名字）：

```
我叫派蒙，桌面陪伴 AI，活泼可爱、口语自然、回复 1-2 句话不超过 50 字。
能力：
- 简单问答、闲聊、查时间天气：直接回答
- 用户问「我的」知识库、笔记、wiki、memory：调 claude_code 工具异步查
- 用户让写公众号、写文章、做长内容：调 claude_code_background，立刻口语回复"派蒙这就去写"
- 用户问「派蒙写完了吗」「任务好了没」「查到了吗」：调 claude_code_check_results
- 控制设备：点头摇头亮灯角度调整用对应官方工具
不要 markdown / 列表 / emoji（要朗读出来）。
```

---

## 工程坑（已踩过帮你避免）

1. **bridge 必须 launchd 守护** —— 不然 Mac 睡眠/重启后 bridge 断了，Stack-chan 的 `claude_code` 工具调用就会超时
2. **claude_code 必须异步** —— xiaozhi.me 的 wss 30 秒不活动就 keepalive timeout，但 `claude --print` 跑知识库查询通常要 60-120 秒。所以工具立刻返回回执，后台跑完写到 outbox，下次询问时再取
3. **xiaozhi.me 官方控制台不能换 LLM 主体** —— 主对话模型必须从虾哥列表里选（DeepSeek/Qwen/豆包），Claude 只能通过 MCP 工具"被调用"，不能作主对话
4. **绑定设备的坑** —— 见上文步骤 3 注解
5. **改固件让 servo 角度稳住要改 3 处** —— 详见 [memory: stack-chan-xiaozhi-servo-rebound](docs/servo-rebound-debug.md)

---

## 限制

- **Stack-chan 任何时候只能服务一个智能体**。这个项目把它绑给"派蒙"这个智能体（带 claude_code MCP），如果你还想用别的智能体（比如官方默认的"台湾女友"），需要在 xiaozhi.me 重新切换设备绑定
- **`claude_code` 工具调用延迟** —— 异步模式下用户感知约 5 秒（"派蒙正在查"立刻出来）+ 实际查询 30-120 秒后才能取结果。这是 Claude Code 启动 + Anthropic API 调用的成本，不可压缩
- **Claude Code 在 Mac 不开机就不能用** —— bridge 需要本机进程 spawn `claude` 命令。如果想远程也用，需要把 Mac 通过 cpolar/frp 暴露公网

---

## 致谢

- 上游固件 [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — 小智 AI 项目本体
- 上游服务端 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（可选）
- M5Stack 出的 [StackChan AI Desktop Robot Kit](https://shop.m5stack.com/products/m5stack-cores3-stackchan-ai-desktop-robot-kit) 硬件

## License

MIT
