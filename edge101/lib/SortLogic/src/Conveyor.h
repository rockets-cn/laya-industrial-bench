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

// 下行命令 (网关 -> 设备). 幂等目标态语义:
//   start/resume -> running (running 时 ok 只更新 speed; paused 时 start 等同 resume;
//                   stopped 时 resume -> busy)
//   stop         -> stopped (已停也 ok)
//   pause        -> paused (已暂停 ok; stopped/fault -> busy)
//   reset        -> stopped (fault 时执行; 非故障 no-op ok)
// 注: 网关把 busy-ack 解释为 "设备正在执行本 job" 并延长等待, 因此
// "已处于目标态" 必须回 ok, 否则网关会空等 busy_wait.
class Conveyor {
 public:
    DevState st = DevState::kStopped;
    double speed_mps = 0.0;
    uint64_t steps_total = 0;   // 遥测用 (对应 sim 的 items_in)

    CmdResult handleCmd(const char* action, double speed) {
        CmdResult r;
        if (!action) return r;
        if (!strcmp(action, "start") || !strcmp(action, "resume")) {
            if (st == DevState::kFault) {
                r.handled = true; r.note = "state=fault"; return r;
            }
            if (!strcmp(action, "resume") && st == DevState::kStopped) {
                r.handled = true; r.note = "state=stopped"; return r;
            }
            if (st != DevState::kRunning) st = DevState::kRunning;
            speed_mps = speed;
            r.handled = r.ok = true; r.note = "state=running";
            return r;
        }
        if (!strcmp(action, "stop")) {
            if (st == DevState::kFault) {
                r.handled = true; r.note = "state=fault"; return r;
            }
            if (st != DevState::kStopped) { st = DevState::kStopped; speed_mps = 0.0; }
            r.handled = r.ok = true; r.note = "state=stopped";
            return r;
        }
        if (!strcmp(action, "pause")) {
            if (st == DevState::kRunning) st = DevState::kPaused;
            else if (st != DevState::kPaused) {
                r.handled = true;
                r.note = std::string("state=") + stateName(st);
                return r;
            }
            r.handled = r.ok = true; r.note = "state=paused";
            return r;
        }
        if (!strcmp(action, "reset")) {
            if (st == DevState::kFault) { st = DevState::kStopped; speed_mps = 0.0; }
            r.handled = r.ok = true;
            r.note = std::string("state=") + stateName(st);
            return r;
        }
        return r;   // 未知 action: 不处理 (不回 busy)
    }

    void setFault() { st = DevState::kFault; speed_mps = 0.0; }
    const char* state() const { return stateName(st); }
};

}  // namespace sort
