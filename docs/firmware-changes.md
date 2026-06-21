# 修固件的具体改动

本项目固件 fork：[heavenchenggong/StackChan-XiaoZhi (codex-refactor branch)](https://github.com/heavenchenggong/StackChan-XiaoZhi/tree/codex-refactor)，基于 [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 上游。

完整 diff 见 [GitHub Compare](https://github.com/heavenchenggong/StackChan-XiaoZhi/commits/codex-refactor)，下面按主题列改动点。

## 1. 加 4 个 servo MCP 工具

**位置**：`main/boards/m5stack-core-s3/m5stack_core_s3.cc::RegisterServoMcpTools()`

```cpp
mcp.AddTool("self.head.move",
    "Move the head/face to a specific angle and HOLD it there until user says "
    "to re-center. ...",
    PropertyList({
        Property("yaw", kPropertyTypeInteger, -45, 45),
        Property("pitch", kPropertyTypeInteger, 5, 60),
    }),
    [this, move_with_pause](const PropertyList& props) -> ReturnValue {
        int yaw = props["yaw"].value<int>();
        int pitch = props["pitch"].value<int>();
        move_with_pause(yaw, pitch, 800);
        return true;
    });

mcp.AddTool("self.head.center",
    "Move the head back to center/forward position AND release the manual lock "
    "so face tracking can resume. ...",
    PropertyList(),
    [this](const PropertyList&) -> ReturnValue {
        face_tracker_.SetManualLock(true);
        face_tracker_.Pause(false);
        servo_.PauseScan();
        servo_.Center();
        // 800ms 后解锁 + 恢复 tracker
        struct UnlockCtx { FaceTracker* t; StackChanServo* s; };
        auto* ctx = new UnlockCtx{&face_tracker_, &servo_};
        xTaskCreate([](void* arg) {
            auto* c = static_cast<UnlockCtx*>(arg);
            vTaskDelay(pdMS_TO_TICKS(800));
            c->t->SetManualLock(false);
            c->t->Resume();
            c->s->ResumeScan();
            delete c;
            vTaskDelete(nullptr);
        }, "center_unlock", 2048, ctx, 2, nullptr);
        return true;
    });

mcp.AddTool("self.head.nod", "Nod the head ...", ...);
mcp.AddTool("self.head.shake", "Shake the head ...", ...);
```

调用点在 board init 末尾：
```cpp
RegisterLedMcpTools();
RegisterExpressionMcpTool();
RegisterServoMcpTools();  // 新加
```

## 2. FaceTracker 加 manual_lock_

**位置**：`main/boards/m5stack-core-s3/m5stack_core_s3.cc::FaceTracker`

```cpp
class FaceTracker {
    ...
    volatile bool manual_lock_ = false;  // 新加

    void Resume() {
        if (manual_lock_) return;  // 新加：锁住时忽略
        if (paused_) {
            paused_ = false;
            has_prev_ = false;
            servo_->PauseScan();
        }
    }

    void SetManualLock(bool locked) { manual_lock_ = locked; }  // 新加
    bool IsManualLocked() const { return manual_lock_; }        // 新加
};
```

**为什么需要**：Application::OnTick 在 listening/speaking 状态会每秒调 `face_tracker_->Resume()`。LLM 把头转到 -30 后，1 秒内 Application Resume 让 FaceTracker 恢复，再用人脸位置覆盖 servo。

## 3. StackChanServo 记当前位置

**位置**：`main/boards/m5stack-core-s3/m5stack_core_s3.cc::StackChanServo`

```cpp
class StackChanServo {
    ...
    int last_yaw_deg_ = 0;       // 新加
    int last_pitch_deg_ = 30;    // 新加

    void MoveTo(int yaw_deg, int pitch_deg, int time_ms) {
        // ...原写 servo 代码不变...
        last_yaw_deg_ = yaw_deg;     // 新加：记 servo 当前命令位置
        last_pitch_deg_ = pitch_deg;
    }

    int GetCurrentYaw() const { return last_yaw_deg_; }      // 新加
    int GetCurrentPitch() const { return last_pitch_deg_; }  // 新加
};
```

**为什么需要**：原版 `Nod()` 用 `tracker_->GetYaw()` 作 base：

```cpp
void StackChanServo::Nod() {
    if (anim_running_) return;
    anim_running_ = true;
    auto* ctx = new ServoAnimCtx{this,
        tracker_ ? (int)tracker_->GetYaw() : 0,    // ← bug: tracker yaw=人脸追踪目标, 不是 servo 实际位置
        tracker_ ? (int)tracker_->GetPitch() : 30};
    ...
}
```

LLM 把 servo 设到 -30 但没改 tracker 内部 yaw_，所以 Nod 用 tracker yaw=0 作 base，把头甩回中间才点头。

**修复**：

```cpp
void StackChanServo::Nod() {
    if (anim_running_) return;
    anim_running_ = true;
    auto* ctx = new ServoAnimCtx{this, GetCurrentYaw(), GetCurrentPitch()};  // 用 servo 当前命令位置
    ...
}
```

Shake / Tilt 同样修法。

## 4. 禁用 light sleep

**位置**：`main/boards/m5stack-core-s3/m5stack_core_s3.cc::InitializePowerSaveTimer()`

```cpp
// 原:
power_save_timer_ = new PowerSaveTimer(-1, 30, -1);

// 改:
power_save_timer_ = new PowerSaveTimer(-1, -1, -1);
```

`PowerSaveTimer` 的第二个参数是 `seconds_to_sleep`，30 秒空闲后进 light sleep（关麦克风、关屏背光）。设备进入 light sleep 后唤醒词检测和音频输入都被禁用：

```cpp
// power_save_timer.cc::PowerSaveCheck() 进 sleep 时：
audio_service.EnableWakeWordDetection(false);
codec->EnableInput(false);
esp_pm_configure({.light_sleep_enable = true});
```

所以"你好小智"在 sleep 后无效，只能触摸屏幕（GPIO 中断）唤醒。

改成 -1 永不 sleep，麦克风一直在线。stack-chan 是有线供电，CPU 略多功耗不要紧。

## 5. GitHub Actions Build CI

**位置**：`.github/workflows/build-cores3.yaml`

完整文件见 [build-cores3.yaml](https://github.com/heavenchenggong/StackChan-XiaoZhi/blob/codex-refactor/.github/workflows/build-cores3.yaml)。

关键点：
- 用 `espressif/idf:v5.5.2` 官方 Docker 镜像 build
- **Stash 作者改过的 wifi html → 清 managed_components → 让 idf.py 重新下 → 覆盖 html 回来 → build**（必须，否则 component_hash 缺失报错）
- App binary 文件名是 `StackChan-XiaoZhi.bin`（CMake project name），不是 `xiaozhi.bin` —— 烧录时要 `cp build/StackChan-XiaoZhi.bin out/xiaozhi.bin`
- 还要烧 `generated_assets.bin` 到 `0xa10000`（mmap 表情/字体素材）

## Diff 统计

| 文件 | 加 | 删 |
|---|---|---|
| `main/boards/m5stack-core-s3/m5stack_core_s3.cc` | ~120 | ~10 |
| `.github/workflows/build-cores3.yaml` | 70 | 0 |

整体不到 200 行改动。

## 兼容性

这些改动**只针对 M5Stack CoreS3 stack-chan 套件**（board=m5stack-core-s3）。其他 board 的代码完全没动。
