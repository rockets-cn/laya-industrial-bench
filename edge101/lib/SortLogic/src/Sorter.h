// 纯逻辑: 分拣器 (舵机拨料) 状态机 (与 sim_devices.SorterSim 语义一致).
// 无 Arduino 依赖.
//
// 下行命令:
//   divert (trigger=immediate, 默认)  ready -> busy (连续执行 qty 次拨料, 完成 -> ready)
//   divert (trigger=photo)            ready -> armed (光门预置: 物品过光门才开始拨;
//                                          qty>1 时每件各等一次光门: 拨完一件
//                                          回 armed 等下一件的落沿;
//                                          kPhotoTimeoutMs 内无物品 -> fault
//                                          note="photo timeout")
//   armed --光门沿--> busy (一次落沿 = 一件)
//   reset  fault -> ready;  armed -> ready (取消预置, 不拨料)
//   其余 -> busy-ack (设备正在执行中)
//
// 光门安装 (时序关键): 装在推杆推料点【前侧 (上游)】, 距离 =
//   带速 × (推出动作时间 + 余量)   (公式与标定见 docs/sort-mqtt.md / wiring.md)
#pragma once
#include <cstdint>
#include <cstring>
#include <string>
#include "Conveyor.h"

namespace sort {

// 光门等待窗: arm 后超过该时间无物品过光门 -> fault (期望的物品没来).
// 5s 覆盖: 带速 1m/s 时 5m 行程 + 网关重传/抖动余量.
constexpr uint32_t kPhotoTimeoutMs = 5000;

class Sorter {
 public:
    int n = 0;                 // 1..3
    std::string state_str = "ready";   // ready | armed | busy | fault
    uint32_t diverted_total = 0;
    std::string last_material;
    std::string last_note;      // 最近一次 fault/busy 的说明 (遥测/诊断用)

    // 待执行的拨料 (armed/busy 期间)
    bool pending = false;
    std::string pend_material;
    int pend_qty = 0;
    int done_count = 0;
    std::string pend_job_id;    // 关联的网关 job_id (超时 fault 时回 ack 用)
    uint32_t armed_at_ms = 0;   // trigger=photo 时记录 arm 时刻
    bool pend_photo = false;    // 本 job 是否光门触发 (多件时每件等一次光门)

    Sorter() = default;
    explicit Sorter(int n_) { n = n_; }

    CmdResult handleCmd(const char* action, const char* material, int qty,
                        const char* trigger = "immediate",
                        const char* job_id = "", uint32_t now_ms = 0) {
        CmdResult r;
        if (action && !strcmp(action, "reset")) {
            if (state_str == "fault") {
                state_str = "ready";
                pending = false;
                r.handled = r.ok = true;
                last_note.clear();
                return r;
            }
            if (state_str == "armed") {
                // 取消光门预置: 不拨料, 回 ready
                state_str = "ready";
                pending = false;
                r.handled = r.ok = true;
                r.note = "cancelled";
                return r;
            }
            // ready/busy: no-op ok (与传送带幂等目标态一致)
            r.handled = r.ok = true;
            return r;
        }
        if (!(action && !strcmp(action, "divert"))) {
            return r;
        }
        if (state_str == "fault") {
            r.handled = true; r.ok = false;
            last_note = "sorter fault";
            r.note = "sorter fault";
            return r;
        }
        if (state_str == "busy" || state_str == "armed") {
            r.handled = true; r.ok = false;
            last_note = "state=" + state_str;
            r.note = last_note;
            return r;
        }
        // ready -> (armed | busy)
        pending = true;
        pend_material = material ? material : "";
        pend_qty = qty < 1 ? 1 : qty;
        done_count = 0;
        pend_job_id = job_id ? job_id : "";
        bool photo = trigger && !strcmp(trigger, "photo");
        pend_photo = photo;
        if (photo) {
            state_str = "armed";   // 等光门
            armed_at_ms = now_ms;
        } else {
            state_str = "busy";    // 立即执行 (默认/回退)
        }
        r.handled = r.ok = true;
        return r;
    }

    // 光门被物品通过 (每分拣器 1 路, 由主循环喂). 仅 armed 时生效.
    // 返回 true = 本次沿触发了本分拣器 (ready->busy 迁移).
    bool onGateEdge(uint32_t now_ms) {
        (void)now_ms;
        if (state_str != "armed") return false;
        state_str = "busy";
        return true;
    }

    // 主循环 tick: armed 超时无物品 -> fault. 返回 true = 本次进入 fault
    // (调用方需对 pend_job_id 回 fault ack).
    bool tick(uint32_t now_ms) {
        if (state_str == "armed" && now_ms - armed_at_ms >= kPhotoTimeoutMs) {
            state_str = "fault";
            pending = false;
            last_note = "photo timeout";
            return true;
        }
        return false;
    }

    // 固件主循环: 每次舵机完成一次拨料调用. 返回是否全部完成.
    // photo job: 一件拨完回 armed 等下一件的光门 (件间由光门节奏控制);
    // immediate job: 保持 busy 连续拨.
    bool stepOne(uint32_t now_ms = 0) {
        if (!pending) return true;
        done_count++;
        if (done_count >= pend_qty) {
            diverted_total += pend_qty;
            last_material = pend_material;
            pending = false;
            state_str = "ready";
            return true;
        }
        if (pend_photo) {
            state_str = "armed";
            armed_at_ms = now_ms;   // 下一件的光门等待窗从此刻重计
        } else {
            state_str = "busy";
        }
        return false;
    }

    void setFault(const char* note = "fault") {
        state_str = "fault";
        pending = false;
        last_note = note ? note : "fault";
    }
    const char* state() const { return state_str.c_str(); }
    const char* note() const { return last_note.c_str(); }
    const char* mat() const { return last_material.c_str(); }
    int pendingQty() const { return pending ? pend_qty - done_count : 0; }
    const std::string& pendingJobId() const { return pend_job_id; }
    std::string topic_cmd() const { return "sort/sorter" + std::to_string(n) + "/cmd"; }
    std::string topic_ack() const { return "sort/sorter" + std::to_string(n) + "/ack/"; }
    std::string topic_tele() const { return "sort/sorter" + std::to_string(n) + "/telemetry"; }
};

}  // namespace sort
