// EDGE 101 (DFR1283, SKU 4G CAT1) — 引脚/网络/参数配置
// 依据: DFRobot wiki DFR1283 (2026-10-29 核对)
//
// 板级事实 (wiki「技术规格」):
//   - ESP32 (原版, 双核 LX6 @240MHz, 16MB 片上 Flash, 520KB SRAM, 无 PSRAM)
//   - 板载 RJ45 以太网 10/100M (MDIO PHY, arduino-esp32 ETH.begin() 自动识别)
//   - 11 路原生 GPIO (40PIN 扩展口): P5 P12 P14 P15 P18 P23 P33 P34 P37 P38 P39
//     其中 P34/P37/P38/P39 只能输入 → 可用输出: GPIO5/12/14/15/18/23 (6 路)
//   - I2C 默认: SDA=GPIO18, SCL=GPIO23 (Arduino Wire)
//   - 串口: Serial(USB, 下载/调试), Serial1(4G 模组占用), Serial2(RS485)
//   - 供电: DC 9-26V 端子 或 USB 5V/2A; 静态 ~100mA@5V
//   - 板载 LED = GPIO15 (低电平点亮), 板载按钮 = GPIO38 (低电平按下)
//
// 引脚分配 (6 路可用输出全用):
//   GPIO5  = HL2 UART TX   (Serial2 重映射, 38400)
//   GPIO12 = HL2 UART RX
//   GPIO14 = 步进 STEP
//   GPIO15 = 步进 DIR      (复用板载 LED: DIR=LOW 时 LED 亮, 可当方向指示)
//   GPIO18 = I2C SDA -> PCA9685 舵机控制器 (3 路舵机, 通道 0/1/2)
//   GPIO23 = I2C SCL
//   步进 EN: 无空闲 GPIO → 在驱动器端把 EN 接 5V (常使能, 靠停脉冲断电逻辑不执行)
#pragma once

#include <cstdint>
#include <IPAddress.h>
#include "HuskyLens.h"

// ---------- HuskyLens 2 (Gravity UART, 38400) ----------
static const uint8_t  HL2_RX_PIN   = 12;    // 板 RX <- HL2 TX (HuskyLens 默认 38400)
static const uint8_t  HL2_TX_PIN   = 5;     // 板 TX -> HL2 RX
static const uint32_t HL2_BAUD     = 38400;
static const uint32_t HL2_RETRIGGER_MS = 500;  // 同物料进画面去重窗口

// ---------- 步进电机 (42 两相 + MKS SERVO42C 闭环 PCBA, 传送带) ----------
// 驱动器: Makerbase SERVO42C — 板载 16384 线磁编码器, FOC 矢量三环,
// 开环/矢量闭环/串口控制三模式; 文档: CSDN article 120565771。
// 我们用 STEP/DIR 驱动 (矢量闭环模式), 接线与 A4988 兼容; 串口控制模式
// 需要第三路 UART, 板上无空闲, 暂不用。
static const uint8_t STEPPER_STEP_PIN = 14;
static const uint8_t STEPPER_DIR_PIN  = 15;   // 复用板载 LED (DIR=LOW -> LED 亮)
// 无 EN 引脚 (见文件头说明): 驱动器 EN 端接 5V 常使能.
// 步/mm = (200 步/圈 × 细分数) ÷ 导程 mm/圈。
// 假设: 1.8° 电机 (200 步/圈) + 导程 10mm + 驱动器设 1/16 细分
//   => 3200 步/圈 ÷ 10mm/圈 = 320 步/mm。
// 电机实际导程与驱动器细分设置确定后, 此值必须同步改!
static const double  STEPS_PER_MM = 320.0;

// ---------- 舵机 ×3 (PCA9685 I2C @0x40, 通道 0/1/2) ----------
static const uint8_t SERVO_SDA_PIN  = 18;
static const uint8_t SERVO_SCL_PIN  = 23;
static const uint8_t SERVO_ADDR     = 0x40;
static const uint8_t SERVO_CH[3]    = {0, 1, 2};
static const int     SERVO_REST_DEG  = 0;    // 归位
static const int     SERVO_EJECT_DEG = 90;   // 拨出
static const uint32_t SERVO_SWEEP_MS   = 800;  // 一次扫掠总时长 (拨出 400 + 归位 400)
static const uint32_t SERVO_ITEM_GAP_MS = 900; // 件间间隔 (>= 扫掠时长)

// ---------- 板载以太网 (RJ45, 网线直连) ----------
// PHY: IP101 (TLK110 驱动), 地址 1; MDC=GPIO4 MDIO=GPIO13 PWR=GPIO2,
// 50MHz 参考时钟 GPIO0 — 全部板内走线 (参数出自 DFRobot 官方 SDK 1.0.1).
// 拓扑: 本机(192.168.2.1/24, broker :1883) <--网线--> EDGE101(192.168.2.2)
static const IPAddress EDGE_IP(192, 168, 2, 2);
static const IPAddress EDGE_MASK(255, 255, 255, 0);
static const IPAddress EDGE_GW(192, 168, 2, 1);
static const IPAddress EDGE_DNS(192, 168, 2, 1);
static const IPAddress BROKER_IP(192, 168, 2, 1);
static const uint16_t  BROKER_PORT = 1883;
static const char* MQTT_CLIENT_ID = "edge101-sorter";

// ---------- 物料映射 (HuskyLens {object_type, ID} -> 网关白名单物料名) ----------
// 色块: object_type=1, ID=颜色块号 (HuskyLens 颜色块识别, 1..4)
// 自定义模型 (YOLO/基础分类): object_type=5, ID=模型里的类别序号
// 物料名必须与 config/sort_gateway.json 白名单一致.
static const sort::MatEntry MATERIAL_MAP[] = {
    {1, 1, "red_block"},      // 红色块 -> 2 号料道 (占位, 待现场确认)
    {1, 2, "green_block"},    // 绿色块
    {1, 3, "blue_block"},
    {1, 4, "yellow_block"},
    {5, 10, "banana"},        // 示例: 自定义模型 ID10
};
static const int MATERIAL_MAP_LEN = sizeof(MATERIAL_MAP) / sizeof(MATERIAL_MAP[0]);
