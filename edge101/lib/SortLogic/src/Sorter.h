// 纯逻辑: 分拣器 (舵机拨料) 状态机 (与 sim_devices.SorterSim 语义一致).
// 无 Arduino 依赖.
#pragma once
#include <cstdint>
#include <cstring>
#include <string>
#include "Conveyor.h"

namespace sort {

// 下行命令:
//   divert ready -> busy (执行 qty 次拨料, 完成 -> ready, diverted_total += qty)
//   reset  fault -> ready
//   其余 -> busy
class Sorter {
 public:
    int n = 0;                 // 1..3
    std::string state_str = "ready";   // ready | busy | fault
    uint32_t diverted_total = 0;
    std::string last_material;

    // 待执行的拨料 (busy 期间)
    bool pending = false;
    std::string pend_material;
    int pend_qty = 0;
    int done_count = 0;

    Sorter() = default;
    explicit Sorter(int n_) { n = n_; }

    CmdResult handleCmd(const char* action, const char* material, int qty) {
        CmdResult r;
        if (action && !strcmp(action, "reset") && state_str == "fault") {
            state_str = "ready"; r.handled = r.ok = true;
            return r;
        }
        if (!(action && !strcmp(action, "divert"))) {
            if (action && !strcmp(action, "reset")) r.handled = true;
            return r;
        }
        if (state_str == "fault") {
            r.handled = true; r.ok = false; r.note = "sorter fault"; return r;
        }
        if (state_str == "busy") {
            r.handled = true; r.ok = false; r.note = "state=busy"; return r;
        }
        // ready -> busy
        state_str = "busy";
        pending = true;
        pend_material = material ? material : "";
        pend_qty = qty < 1 ? 1 : qty;
        done_count = 0;
        r.handled = r.ok = true;
        return r;
    }

    // 固件主循环: 每次舵机完成一次拨料调用. 返回是否全部完成.
    bool stepOne() {
        if (!pending) return true;
        done_count++;
        if (done_count >= pend_qty) {
            diverted_total += pend_qty;
            last_material = pend_material;
            pending = false;
            state_str = "ready";
            return true;
        }
        return false;
    }

    void setFault() { state_str = "fault"; pending = false; }
    const char* state() const { return state_str.c_str(); }
    const char* mat() const { return last_material.c_str(); }
    std::string topic_cmd() const { return "sort/sorter" + std::to_string(n) + "/cmd"; }
    std::string topic_ack() const { return "sort/sorter" + std::to_string(n) + "/ack/"; }
    std::string topic_tele() const { return "sort/sorter" + std::to_string(n) + "/telemetry"; }
};

}  // namespace sort
