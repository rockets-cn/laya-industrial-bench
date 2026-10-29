// PCA9685 16 通道 I2C 舵机/LED 控制器驱动 (极简版).
// 用途: 3 路分拣舵机 (EDGE 101 可用 GPIO 不够 3 路 PWM, 走 I2C).
// 50Hz PWM: prescale=122 -> 49.99Hz, 周期 20000us.
// 角度映射: 0° = 500us, 180° = 2500us (标准舵机).
// 接线: SDA=GPIO18 SCL=GPIO23 (板载默认 I2C), 供电独立 5V, GND 共地.
//       PCA9685 模块若有 VDDIO 跳线, 拨到 3.3V (I2C 电平与 ESP32 一致).
#pragma once
#include <Arduino.h>
#include <Wire.h>

class ServoI2C {
 public:
    static constexpr uint8_t kAddr = 0x40;
    static constexpr int kPrescale50Hz = 122;   // 25MHz/(4096*122) = 49.99Hz

    bool begin(uint8_t sda, uint8_t scl) {
        Wire.begin(sda, scl);
        Wire.setClock(400000);
        // 退出 SLEEP (MODE1 bit4) — 上电后必须先清 SLEEP 才能写 PRESCALE
        uint8_t m = readReg(0x00);
        m &= 0x70;                             // 清 SLEEP, 其它位不动
        if (writeReg(0x00, m) != 0) return false;
        delay(2);
        if (writeReg(0xFE, kPrescale50Hz) != 0) return false;
        delay(5);
        return true;
    }

    // 写占空比 (0..4095; 4095 = 满占空, 0 = 关)
    int setDuty(uint8_t ch, uint16_t duty) {
        return writeBlock(ch, duty, 0, duty);
    }

    // 写脉宽 (us, 50Hz)
    int setPulse(uint8_t ch, uint16_t us) {
        uint32_t duty = (uint32_t)us * 4096 / 20000;
        if (duty > 4095) duty = 4095;
        return setDuty(ch, (uint16_t)duty);
    }

    // 角度 0..180 -> 脉宽 500..2500us
    int setAngle(uint8_t ch, int deg) {
        if (deg < 0) deg = 0;
        if (deg > 180) deg = 180;
        uint16_t us = 500 + (uint16_t)((uint32_t)deg * 2000 / 180);
        return setPulse(ch, us);
    }

 private:
    int writeReg(uint8_t reg, uint8_t val) {
        Wire.beginTransmission(kAddr);
        Wire.write(reg);
        Wire.write(val);
        return Wire.endTransmission();
    }

    // 一次事务写 4 字节: ON_L ON_H OFF_L OFF_H (不自动递增)
    int writeBlock(uint8_t ch, uint16_t on, uint16_t off) {
        uint8_t reg = 0x04 + 4 * ch;           // LED0_ON_L 起
        Wire.beginTransmission(kAddr);
        Wire.write(reg);
        Wire.write(on & 0xFF);
        Wire.write(on >> 8);
        Wire.write(off & 0xFF);
        Wire.write(off >> 8);
        return Wire.endTransmission();
    }

    uint8_t readReg(uint8_t reg) {
        Wire.beginTransmission(kAddr);
        Wire.write(reg);
        if (Wire.endTransmission(false) != 0) return 0;
        Wire.requestFrom(kAddr, (int)1);
        return Wire.available() ? Wire.read() : 0;
    }
};
