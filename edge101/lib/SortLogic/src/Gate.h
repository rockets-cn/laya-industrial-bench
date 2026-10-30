// 纯逻辑: 光门 (光电开关) 边沿检测 + 去抖. 无 Arduino 依赖.
//
// 硬件模型: NPN 开集 + 上拉. 无物品=HIGH, 物品挡光=LOW.
// 有效沿 = 落沿 (H->L): 物品到达推杆前侧.
//
// 语义: 一次 LOW 序列只报 1 个沿 — 在 LOW 持续 >= kDebounceMs 后确认,
// 物品停在门前/振动不重复报; 物品离开 (回 HIGH) 后复位, 下次挡光
// 才算新物品. 主循环每 ~5-10ms 读一次电平喂入.
#pragma once
#include <cstdint>

namespace sort {

constexpr uint32_t kGateDebounceMs = 10;

class PhotoGate {
 public:
    // 返回 true = 本次 poll 确认了一个"物品到达"落沿
    bool onItemEdge(bool level_high, uint32_t now_ms) {
        if (level_high) {
            level_ = true;      // 物品离开: 复位
            fired_this_low_ = false;
            return false;
        }
        if (level_) {
            // 新 LOW 序列第 1 次: 开始计时
            level_ = false;
            low_since_ms_ = now_ms;
            return false;
        }
        // 持续 LOW
        if (fired_this_low_) return false;
        if (now_ms - low_since_ms_ < kGateDebounceMs) return false;
        fired_this_low_ = true;
        return true;
    }

    // 诊断: 当前物品是否挡在门前
    bool itemPresent() const { return !level_; }

 private:
    bool level_ = true;          // 最近一次确认的电平 (true=HIGH=无物品)
    uint32_t low_since_ms_ = 0;  // 当前 LOW 序列起点
    bool fired_this_low_ = false;
};

}  // namespace sort
