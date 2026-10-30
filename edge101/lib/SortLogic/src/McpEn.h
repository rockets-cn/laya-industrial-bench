// MCP23017 数字 IO 扩展 — 步进驱动器 EN 控制.
//
// 背景: EDGE 101 仅 6 路可用输出 (HL2 UART 2 + 步进 STEP/DIR 2 + I2C 2),
// 没有 GPIO 直接控制驱动器 EN. MCP23017 与 PCA9685 共用 I2C 总线
// (P18/P23), 默认地址 0x27 (拨码 A2A1A0 全 1), PA0 -> 驱动器 EN。
//
// 为什么用它管 EN 而不是舵机: EN 是慢变数字量, 数字 IO 扩展的本职;
// 舵机需要 50Hz 硬件 PWM, 仍走 PCA9685 (见 ServoI2C.h)。
// 板上没插 MCP23017 时, EN 保持外部跳线 (接 5V 常使能), 固件自动检测回退。
//
// 纯 C++ (无 Arduino 依赖): I2C 寄存器传输经 I2cReg 注入, 主机单测可测。
#pragma once
#include <cstdint>
#include <cstring>

namespace sort {

// 最小 I2C 寄存器传输接口 (ESP32 侧用 TwoWire 实现, 主机测试用 fake 实现)
class I2cReg {
 public:
    virtual ~I2cReg() = default;
    virtual bool alive() = 0;          // 总线探测 (地址应答)
    virtual void writeReg(uint8_t reg, uint8_t val) = 0;
    virtual uint8_t readReg(uint8_t reg) = 0;   // 读失败返回 0xFF
};

// 传送带状态 -> EN 是否应使能 (纯策略, 主机可测)
// 只有 running 需要线圈通电; stopped/paused/fault 全部禁能 (省电 + 停机安全)。
struct EnPolicy {
    static bool enabledFor(const char* state) {
        return state != nullptr && std::strcmp(state, "running") == 0;
    }
};

class McpEn {
 public:
    // MCP23017 寄存器 (Port A): IODIR_A=0x00, GPIO_A=0x12
    static constexpr uint8_t kAddr = 0x27;
    static constexpr uint8_t kIodirA = 0x00;
    static constexpr uint8_t kGpioA = 0x12;
    static constexpr uint8_t kEnMask = 0x01;   // PA0
    // EN 极性: false = 高电平使能 (A4988/DRV8825 惯例)。
    // 若 SERVO42C 的 EN 是低电平使能, 在 config.h 注释处同步改为 true。
    static constexpr bool kActiveLow = false;

    // 探测芯片; 在线则把 PA0 配成输出并按当前传送带状态初始化电平。
    // 返回 true = 在线且已接管 EN。
    bool begin(I2cReg* bus, const char* conveyor_state) {
        bus_ = bus;
        present_ = false;
        level_ = false;
        if (bus_ == nullptr || !bus_->alive()) return false;
        bus_->writeReg(kIodirA, kEnMask);            // PA0 -> 输出
        apply(EnPolicy::enabledFor(conveyor_state));
        present_ = true;
        return true;
    }

    // 状态机驱动: 电平有变化才写 I2C。每 loop 调一次无成本 (通常 0 次写)。
    bool sync(const char* conveyor_state) {
        if (!present_) return false;
        apply(EnPolicy::enabledFor(conveyor_state));
        return level_;
    }

    // 手动强制电平 (串口控制台 stepper 命令: 临时使能脉冲, 发完恢复)。
    void forceLevel(bool enabled) {
        if (present_) apply(enabled);
    }

    bool present() const { return present_; }
    bool level() const { return level_; }   // 线上电平 (true=高)

 private:
    void apply(bool want_enabled) {
        const bool want_line = want_enabled ^ kActiveLow;
        if (want_line != level_) {
            bus_->writeReg(kGpioA, want_line ? kEnMask : 0x00);
            level_ = want_line;
        }
    }

    I2cReg* bus_ = nullptr;
    bool present_ = false;
    bool level_ = false;
};

}  // namespace sort
