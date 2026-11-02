// BeltHold.h — 推料期间停带 (belt-hold) 状态机.
//
// 物理口径: 推杆扫掠 (拨出一件) 期间传送带必须停止 — 物品停在推料点,
// 被推杆拨入料道; 扫掠完成 (一件或一个 job 的当前扫掠结束) 后, 若传送带
// 仍处 running 态则自动恢复. 多分拣器可能并发扫掠, 用引用计数 (hold) 表示:
// 全部扫掠结束 (hold 归零) 才允许起带.
//
// 与 Conveyor (网关指令态) 的关系: Conveyor 记录「网关想让带子处于什么态」,
// BeltHold 记录「带子此刻实际是否在转」— 推料 hold 是隐式停带, 不改 Conveyor
// 状态, 只在 hold 结束且 Conveyor 为 running 时自动恢复.
//
// 主机可单测 (tests/host); ESP32 端 main.cpp 以 start/stopStepRmt() 执行.
#pragma once

namespace sort {

struct BeltHold {
    int hold = 0;        // 进行中的扫掠数 (引用计数, 0 = 无推料)
    bool running = false;  // 网关指令态: running (start/resume 后为 true)
    bool on = false;      // 实际: 步进是否在转

    // 扫掠开始 (推杆推出): 停带. 幂等安全 — 带子没转也不报错.
    void sweepStart() {
        hold++;
        on = false;
    }
    // 扫掠结束: 无扫掠且网关态 running -> 起带. 返回是否应起带.
    bool sweepEnd() {
        if (hold > 0) hold--;
        if (hold == 0 && running) {
            on = true;
            return true;
        }
        return false;
    }
    // 网关 start/resume: 推料进行中则挂起 (hold 归零时由 sweepEnd 自动起带).
    // 返回是否应立即起带.
    bool conveyorStart() {
        running = true;
        if (hold == 0) {
            on = true;
            return true;
        }
        return false;
    }
    // 网关 stop/pause: 带子停.
    void conveyorStop() {
        running = false;
        on = false;
    }
    // 步进是否应在转 (RMT/回退脉冲共用判据).
    bool beltOn() const { return on && running && hold == 0; }
};

}  // namespace sort
