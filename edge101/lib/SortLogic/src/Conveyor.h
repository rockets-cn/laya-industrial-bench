// 纯逻辑: 传送带状态机 (与 sim_devices.ConveyorSim 语义一致).
// 无 Arduino 依赖, 主机单测与 ESP32 固件共用.
#pragma once
#include <cstdint>
#include <cstring>
#include <string>

namespace sort {

enum class DevState { kStopped, kRunning, kPaused, kFault };

inline const char* stateName(DevState s) {
    switch (s) {
        case DevState::kStopped: return "stopped";
        case DevState::kRunning: return "running";
        case DevState::kPaused:  return "paused";
        case DevState::kFault:   return "fault";
    }
    return "unknown";
}

struct CmdResult {
    bool handled = false;   // false = 未知 action
    bool ok = false;        // true = 接受; false = busy
    std::string note;
};

// 下行命令 (网关 -> 设备):
//   start  stopped -> running   (speed_mps)
//   resume paused  -> running   (speed_mps)
//   pause  running -> paused
//   stop   running/paused -> stopped
//   reset  fault   -> stopped
//   其余状态组合 -> busy (note 带当前状态)
class Conveyor {
 public:
    DevState st = DevState::kStopped;
    double speed_mps = 0.0;
    uint64_t steps_total = 0;   // 遥测用 (对应 sim 的 items_in)

    CmdResult handleCmd(const char* action, double speed) {
        CmdResult r;
        if (action && !strcmp(action, "start") && st == DevState::kStopped) {
            st = DevState::kRunning; speed_mps = speed; r.handled = r.ok = true;
        } else if (action && !strcmp(action, "resume") && st == DevState::kPaused) {
            st = DevState::kRunning; speed_mps = speed; r.handled = r.ok = true;
        } else if (action && !strcmp(action, "pause") && st == DevState::kRunning) {
            st = DevState::kPaused; r.handled = r.ok = true;
        } else if (action && !strcmp(action, "stop") &&
                   (st == DevState::kRunning || st == DevState::kPaused)) {
            st = DevState::kStopped; speed_mps = 0.0; r.handled = r.ok = true;
        } else if (action && !strcmp(action, "reset") && st == DevState::kFault) {
            st = DevState::kStopped; speed_mps = 0.0; r.handled = r.ok = true;
        } else if (action &&
                   (!strcmp(action, "start") || !strcmp(action, "stop") ||
                    !strcmp(action, "pause") || !strcmp(action, "resume") ||
                    !strcmp(action, "reset"))) {
            r.handled = true; r.ok = false;   // 已知 action: 回 busy ack
            r.note = std::string("state=") + stateName(st);
        }
        return r;
    }

    void setFault() { st = DevState::kFault; speed_mps = 0.0; }
    const char* state() const { return stateName(st); }
};

}  // namespace sort
