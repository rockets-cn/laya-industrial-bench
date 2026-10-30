// MCP23017 Port B — 光门 (光电开关) 输入配置与读取.
//
// 背景: 3 个拨料器各需 1 路光门做拨料时序触发 (divert trigger=photo):
// 物品挡光 = LOW, 固件在分拣器 armed 时把落沿当"物品到达拨料器"。
// 板子 6 路 GPIO 全用, 光门走 MCP23017 的 PB0/PB1/PB2 (与 PA0 步进 EN
// 共一个芯片, I2C @0x27, 与 PCA9685 共 P18/P23 总线)。
//
// NPN 开集光门输出拉低, 需要上拉: 开 MCP23017 内部上拉 (GPPU_B)。
// 纯 C++: 寄存器传输经 I2cReg (McpEn.h) 注入, 主机单测可测。
#pragma once
#include <cstdint>
#include "McpEn.h"

namespace sort {

class McpGate {
 public:
    // MCP23017 寄存器 (Port B): IODIR_B=0x01, GPPU_B=0x02, GPIO_B=0x13
    static constexpr uint8_t kIodirB = 0x01;
    static constexpr uint8_t kGppuB  = 0x02;
    static constexpr uint8_t kGpioB  = 0x13;

    // 把 mask 覆盖的 PB 口配成输入 + 内部上拉 (无物品=HIGH)。
    // 返回 true = 芯片在线且配置成功。
    bool begin(I2cReg* bus, uint8_t mask) {
        bus_ = bus;
        present_ = false;
        if (bus_ == nullptr || !bus_->alive()) return false;
        bus_->writeReg(kIodirB, mask);       // 1 = 输入
        bus_->writeReg(kGppuB, mask);        // 内部上拉 (NPN 光门需要)
        present_ = true;
        return true;
    }

    // 一次读 3 路电平 (true = HIGH = 无物品)。芯片离线时全 HIGH (安全侧:
    // 不会误报"有物品", 只会走光门等待超时路径)。
    void poll(bool& hi0, bool& hi1, bool& hi2) {
        hi0 = hi1 = hi2 = true;
        if (!present_) return;
        uint8_t v = bus_->readReg(kGpioB);
        hi0 = (v & 0x01) != 0;
        hi1 = (v & 0x02) != 0;
        hi2 = (v & 0x04) != 0;
    }

    bool present() const { return present_; }

 private:
    I2cReg* bus_ = nullptr;
    bool present_ = false;
};

}  // namespace sort
