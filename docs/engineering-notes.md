# Stack-chan × Claude Code 桥接 — 工程笔记

记录踩过的非显然坑，方便其他人少走弯路。

## 1. xiaozhi.me 官方控制台**不能**换 LLM 主体

`xiaozhi.me` FAQ Q29 明确说："不支持 GPT / Claude / OpenAI 兼容 endpoint 作主对话 LLM"，只能从虾哥列表里选（Qwen / DeepSeek / 豆包 / 小智 Lite）。

但 **MCP 接入点是开放的** —— 也就是说 Claude 不能做"派蒙本人"，但可以做"派蒙的工具"。这就是这个项目的核心抓手。

## 2. 拿 device_id 的方式只有一种 — 语音问设备

虾哥客服处理解绑工单时要 "纯数字 device_id"，但这个 ID 在：

- ❌ 串口启动日志里没有
- ❌ NVS 分区里没有（NVS 里是 `client_id = GID_xxx@@@MAC@@@UUID`，不是 device_id）
- ❌ efuse BLOCK_USR_DATA 默认空
- ✅ **只能语音问设备**："你的设备 ID 是多少？" 设备会语音回答一串约 7 位的数字（例如 `2078585`）

设备没正式激活过（一直在 `GID_test` demo 池）时，问设备也能拿到这个数字 ID，发邮件解绑时附上这个 + MAC 即可。

## 3. xiaozhi.me wss MCP 链接的 keepalive 是 30 秒

`wss://api.xiaozhi.me/mcp/?token=...` 这个 long-lived 连接服务器端每 30 秒发一次 PingRequest。

工具调用的 reply 必须在 30 秒内返回，否则虾哥服务端踢连接（`sent 1011 internal error: keepalive ping timeout`）。

但 `claude --print` 跑一个 memory 查询通常要 30-120 秒。

**解法**：把 `claude_code` 工具默认改成**异步**——立刻返回 "派蒙这就去查" 让 wss 不超时，后台 spawn `claude` 把结果写文件，下次用户问"查到了吗"再 check_results 取。

## 4. servo 控制：三重并发覆盖

把 servo MCP 工具加进去后，"头转过去又弹回原位"问题极难调。三个独立子系统并发覆盖 servo：

### 第 1 重：FaceTracker task 每 ~150ms 写 servo
```cpp
// FaceTracker::Track() 在独立 task 跑
servo_->MoveTo((int)yaw_, (int)pitch_, 150);  // 每 100-150ms
```

**修法**：`face_tracker_.Pause(false)`。
注意 `Pause(bool resume_scan=true)` 默认参数会立刻 `ResumeScan()` 启动 IdleScan，**必须传 false**。

### 第 2 重：Application 状态机周期 Resume

在 `kDeviceStateListening / kDeviceStateSpeaking` 状态下 OnTick 每秒调：
```cpp
if (face_tracker_) face_tracker_->Resume();
```

**修法**：给 FaceTracker 加 `manual_lock_` flag，`Resume()` 头部检查：
```cpp
void Resume() {
    if (manual_lock_) return;  // LLM 锁住时忽略外部 Resume
    ...
}
```

LLM head.move 工具 `SetManualLock(true)` 后，Application 周期 Resume 变 no-op。

### 第 3 重：SetEmotion → 自动 Nod/Shake，用 tracker yaw 作 base

最隐蔽。派蒙说完话切表情 happy → SetEmotion 自动 `servo_->Nod()`。Nod 用 `tracker_->GetYaw()` 作 base（默认 0），不是 servo 实际位置。所以 Nod 把头甩回 yaw=0 才点头。

**修法**：StackChanServo 在 MoveTo 里更新 `last_yaw_deg_ / last_pitch_deg_`，暴露 `GetCurrentYaw / GetCurrentPitch`。Nod/Shake/Tilt 用这个作 base，就地点头：

```cpp
auto* ctx = new ServoAnimCtx{this, GetCurrentYaw(), GetCurrentPitch()};
```

## 5. 调试时 grep 比看 log 高效

碰到"servo 设了又弹"时直接 grep 源码所有 servo MoveTo 调用点，不要光看 log 滤太严会漏调用方：

```bash
grep -nE "servo_?\.\s*MoveTo|servo_?->\s*MoveTo|bus_\.\s*WritePos|\.MoveTo\(" main/
```

m5stack-core-s3 board 里有 7 处调 servo：
- Begin/Center (init 一次)
- IdleScanCb (4s 一次)
- FaceTracker::Track (100ms 一次)
- Nod/Shake/Tilt 动画
- LLM MCP 工具

要同时解决所有，缺一个都会"弹回"。

## 6. PAT workflow scope

`.github/workflows/*.yaml` 文件不能用普通 `repo` scope 的 PAT push。**必须**勾 `workflow` 复选框：
1. https://github.com/settings/tokens/new
2. 勾 `repo` + `workflow`
3. Generate token

否则 git push 时报错：
```
remote rejected: refusing to allow a Personal Access Token to create or update workflow `.github/workflows/*.yaml` without `workflow` scope
```

`gh api PUT contents/.github/workflows/*` 也走不通（同样 scope 限制）。

## 7. 烧 firmware 不要每次都 `merged-binary.bin`

`merged-binary.bin` 从 0x0 开始覆盖整个 flash，**会擦掉 NVS**（WiFi 凭据、激活状态）。

只升级 app 用：
```bash
esptool.py write-flash 0x410000 xiaozhi.bin
```

保留 WiFi 凭据 + 服务器绑定状态，省去每次重新配网 + 等服务器响应。

仅在**初次烧机**或**版本跳跃改 partition layout** 时用 merged-binary。

## 8. GH Actions build 卡点：managed_components hash mismatch

`xiaozhi-esp32` 这个 repo 把 `managed_components/78__esp-wifi-connect/assets/*.html` 强加进 git（作者改过这些 HTML），但没附带 `.component_hash`。CI 里 component manager 看到目录已存在但缺 metadata 就报错：

```
ERROR: File .component_hash or CHECKSUMS.json for component "78/esp-wifi-connect" 
in the managed components directory does not exist or cannot be parsed.
```

**修法**：CI 步骤里先 stash 作者改的 HTML，删整个 `managed_components/`，让 `idf.py` 重新下，然后覆盖回去：

```yaml
- name: Stash author HTML + clean managed_components
  run: |
    mkdir -p /tmp/stash-wifi-html
    cp -r managed_components/78__esp-wifi-connect/assets/* /tmp/stash-wifi-html/ 2>/dev/null || true
    rm -rf managed_components
- name: Build
  run: |
    . $IDF_PATH/export.sh
    idf.py set-target esp32s3
    idf.py reconfigure || true   # 让 component manager 重下
    cp -r /tmp/stash-wifi-html/* managed_components/78__esp-wifi-connect/assets/ || true
    idf.py build
```
