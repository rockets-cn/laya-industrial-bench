// EDGE 101 (DFR1283, SKU 4G CAT1) — 引脚/网络/参数配置
// 依据: DFRobot wiki DFR1283 (2026-10-29 核对)
//
// 板级事实 (wiki「技术规格」):
//   - ESP32 (原版, 双核 LX6 @240MHz, 16MB 片上 Flash, 520KB SRAM, 无 PSRAM)
//   - 板载 RJ45 以太网 10/100M (PHY=IP101, 参数见 src/main.cpp ETH.begin)
//   - 11 路原生 GPIO (40PIN 扩展口): P5 P12 P14 P15 P18 P23 P33 P34 P37 P38 P39
//     其中 P34/P37/P38/P39 只能输入 → 可用输出: GPIO5/12/14/15/18/23 (6 路)
//   - I2C 默认: SDA=GPIO18, SCL=GPIO23 (Arduino Wire)
//   - 串口: Serial(USB, 下载/调试), Serial1(4G 模组占用), Serial2(RS485)
//   - 供电: DC 9-26V 端子 或 USB 5V/2A; 静态 ~100mA@5V
//   - 板载 LED = GPIO15 (低电平点亮), 板载按钮 = GPIO38 (低电平按下)
//
// 引脚分配 (2026-10-30 硬件改线):
//   GPIO5  = 步进 DIR      (LOW=正向: 物品向分拣器方向; 反了则换接线)
//   GPIO12 = 舵机 1 (LEDC PWM 直连; 该脚内部下拉 -> 上电固件接管前可能抖一次归零)
//   GPIO14 = 步进 STEP (RMT 硬件脉冲)
//   GPIO15 = 舵机 2 (LEDC PWM 直连; 板载 LED 复用就此放弃)
//   GPIO18 = I2C SDA (板载 I2C 总线, 预留 HL2)
//   GPIO23 = I2C SCL (板载 I2C 总线, 预留 HL2 — 原与舵机 3 冲突, 已挪到 33 解除)
//   GPIO33 = 舵机 3 (LEDC PWM 直连; 原 U1TXD/4G 复用, 4G 不用, 无影响)
//   步进 EN = 不接线 (驱动器浮空, 2026-11-03 实测可用; 固件不控制 EN)
//   34/37/39 = 只能输入, 预留传感器 (无内部上拉, 用时需外接上拉)
//   38 = 板载按钮 (输入)
#pragma once

#include <cstdint>
#include <IPAddress.h>
#include "HuskyLens.h"

// ---------- HuskyLens 2 ----------
// 现状 (2026-10-30): 计划走 I2C, 尚未连接。板上 I2C 总线 SDA=18 / SCL=23
// 已空闲 (原 SCL 与舵机 3 冲突, 舵机 3 已挪到 33)。
// HL2_ON=0: 本固件不初始化 HL2; 虚拟光门仍可用串口 `vision <material>`
// 注入检测锚点离线验证 (配合 `track` 命令)。
// 接线确认后启用: 实现 I2C 传输 (帧格式与 UART 相同 0x55 0x55, 写 0x1B
// 触发取帧, I2C 地址需实机扫描确认) -> 本宏改 1。
#define HL2_ON 0
static const uint8_t  HL2_I2C_SDA_PIN = 18;   // 板载 I2C (Arduino Wire 默认)
static const uint8_t  HL2_I2C_SCL_PIN = 23;
static const uint32_t HL2_RETRIGGER_MS = 500;  // 同物料进画面去重窗口
#if HL2_ON
// (旧 UART 接线占位, 本轮未用: 5/12 已改作 DIR/舵机 1)
static const uint8_t  HL2_RX_PIN   = 12;
static const uint8_t  HL2_TX_PIN   = 5;
static const uint32_t HL2_BAUD     = 38400;
#endif

// ---------- HuskyLens 2 I2C (DFRobot 官方库 lib/HuskylensV2) — 新方案 ----------
// 识别物料 (数字孪生"真实侧"): 轮询 getResult -> 按 confmin 过滤 -> 发
// sort/inbound/vision. 不做选仓 (选仓权在 PC laya 模型). boot 时切自学习
// 分类算法一次 + 强制等 5s (wiki 警告, 否则系统崩溃).
#define HL_I2C_ON 1
static const uint32_t HL_POLL_MS          = 300;   // I2C 轮询周期 (ms; 越大对主循环停摆越小)
static const uint32_t HL_TIMEOUT_MS       = 200;   // 单次 getResult 超时 (防卡主循环)
static const double   HL_CONFMIN          = 0.6;   // 置信度门槛 (在线 confmin 可调)
static const int      HL_ALGO             = 129;   // 已安装定制模型 (>= ALGORITHM_CUSTOM_BEGIN=128; 129 = 用户 PCB 定制模型)
static const uint32_t HL_SWITCH_WAIT_MS   = 5000;  // 切算法后强制等待 (wiki >=5s)

// ---------- 红外接近传感器 (位置基准 T0, GPIO34; 纯输入, 需外接 3.3V 上拉) ----------
// 物品前缘到 IR = T0; 按带速匀速外推预测到各仓推杆时刻 -> 到点瞬停 + 推杆入库.
// 极性默认接近=LOW (NPN 开漏); 在线 `irset pol` 可翻.
static const uint8_t IR_PIN        = 34;
// 各仓到 IR 触点的实际距离 (标定定案 2026-11-11, 真机 9 连测; 非等间距 1→2=62, 2→3=52).
// 带速标定=准 (52.49mm/s, 过冲系数1.000), 故落点=精确等于 d. main.cpp 用这三个值.
static const double  BIN_DIST_MM_1 = 125.0;   // 1号仓
static const double  BIN_DIST_MM_2 = 187.0;   // 2号仓
static const double  BIN_DIST_MM_3 = 239.0;   // 3号仓
static const double  IR_DIST1_MM   = BIN_DIST_MM_1;   // legacy (irset 在线默认)
static const double  IR_SPACING_MM = 52.0;            // legacy (irset/spset 显示; 选仓已按 BIN_DIST 查表)
static const bool    IR_ACTIVE_LOW = true;    // 接近 = 低电平 (默认 NPN)

// ---------- 步进电机 (42 两相 + MKS SERVO42C 闭环 PCBA, 传送带) ----------
// 驱动器: Makerbase SERVO42C — 板载 16384 线磁编码器, FOC 矢量三环,
// 开环/矢量闭环/串口控制三模式; 文档: CSDN article 120565771。
// 我们用 STEP/DIR 驱动 (矢量闭环模式), 接线与 A4988 兼容; 串口控制模式
// 需要第三路 UART, 板上无空闲, 暂不用。
static const uint8_t STEPPER_STEP_PIN = 14;
static const uint8_t STEPPER_DIR_PIN  = 5;    // LOW=正向 (物品向分拣器方向; 反了换接线)
// EN: 不接线 (2026-11-03 实测 SERVO42C 低电平使能: 浮空=使能, 高电平=禁能;
// 想固化可在驱动器端跳 GND; 固件不控制 EN).
// 步/mm = (200 步/圈 × 细分数) ÷ 导程 mm/圈。
// 历史假设: 1.8° 电机 (200 步/圈) + 导程 10mm + 1/16 细分 => 320 步/mm。
// 2026-11-03 卷尺+基准实测三次: 3200步=46.6mm / 3200步=48.11mm /
// 7000步=99.11mm (±3%) → 加权 13400步÷193.82mm => 69.1 步/mm。
// 细分/导程未确认; 若确认按公式重算再复核。
static const double  STEPS_PER_MM = 69.1;

// ---------- MCP23017 (I2C 数字 IO 扩展, 与 PCA9685 共 P18/P23 总线) ----------
// DFR0626, 默认地址 0x27 (拨码 A2A1A0 全 1); PA0 -> 步进驱动器 EN,
// PB0/PB1/PB2 -> 3 路光门 (拨料时序触发)。
// 地址/极性常量在 lib/SortLogic/src/McpEn.h (kAddr/kActiveLow), 改动需两处同步。
// 剩余 13 路 (PA1..PA7, PB3..PB7) 预留: 料道末端对账光电/限位/急停等数字量。

// ---------- 光门 ×3 (推杆前方, 推出时序触发) ----------
// 透过式光电, NPN 开集输出 + 内部上拉 (McpGate 配 GPPU_B):
// 无物品=HIGH, 物品挡光=LOW。每推杆 1 路: 物品过光门 = "到达推杆前侧"。
// 固件语义: divert 命令带 trigger=photo 时, 分拣器进入 armed 等光门;
// 光门落沿触发拨料; 5s (Sorter.h kPhotoTimeoutMs) 无物品 -> fault
// "photo timeout"。光门未接 / MCP23017 未接时 trigger=photo 先走虚拟光门
// (下一节), 再回退 immediate 执行 (ack note 说明), 保证未装光门也能跑通。
static const bool     PHOTO_GATE_ENABLED = true;
static const uint8_t  PHOTO_GATE_MASK_B  = 0x07;   // PB0+PB1+PB2 -> 1/2/3 号分拣器
static const uint8_t  PHOTO_GATE_BIT[3]  = {0x01, 0x02, 0x04};

// ---------- 虚拟光门 (带面位置跟踪): 无物理光门时的 photo 时序 ----------
// 原理: HL2 检测得到 (t0, 像素位置) -> 物品带面位置 p0 (mm, 相机参考系,
// + = 下游); 匀速外推 p(t) = p0 + v·(t−t0) (v = 传送带当前 speed_mps)。
// divert trigger=photo 且物理光门未接时, 固件预测物品过各推杆光门线
// (GATE_DIST_MM[n] = 相机参考平面 -> n 号光门安装位, 沿带面量) 的时刻,
// 到点推出 -> 与 gate_layout.svg 的时序一致 (物品到光门位置才推)。
// 解析顺序: 物理光门 -> 虚拟光门 (TRACK_ENABLED 且有同物料窗口内检测且
// 带速 > 0) -> immediate 回退 (串口日志说明原因)。
// 标定项 (现场实测, 方法见 edge101/docs/wiring.md "光门/虚拟光门标定"):
//   VIEW_WIDTH_MM  相机视野在带面平面的宽度 (决定像素 -> mm 换算)
//   GATE_DIST_MM   相机 -> 各光门的沿带距离 (安装公式见 gate_layout.svg)
//   ITEM_PITCH_MM  photo job 件距 (>= 带速×单件推出周期, 默认 @1m/s)
static const bool     TRACK_ENABLED         = true;
static const uint16_t VIEW_CX_PX            = 160;   // 图像中心 x (HL2 ~320x240)
static const double   VIEW_WIDTH_PX         = 320.0; // 帧宽 (px)
static const double   VIEW_WIDTH_MM         = 200.0; // 视野在带面平面的宽度 (mm, 标定项)
static const double   GATE_DIST_MM[3]       = {500.0, 500.0, 500.0}; // 相机->1/2/3 光门 (mm, 标定项)
static const double   ITEM_PITCH_MM         = 900.0; // photo job 件距 (mm, 标定项)
static const uint32_t TRACK_MATCH_WINDOW_MS = 3000;  // 视觉 -> divert 匹配窗 (ms)

// ---------- 舵机 ×3 (LEDC 硬件 PWM, 直连; 2026-10-30 自 PCA9685 改直连) ----------
// 50Hz / 15bit, 标准映射 0°=1000µs / 90°=1500µs / 180°=2000µs;
// 宽量程推杆舵机: 收回=270°(us=2500), 展开=-80°(us=556)
// (2026-11-10 用户实机确认; 早期 config 把方向定义反了: 曾误标 收回=-80°/全展开=240°)
// 编号定稿 (2026-11-04 判别测试): 命令 "1 号" 实际推动 3 号仓处杆 → 映射之前写反。
// 实测: 1 号仓(离进口最近) = GPIO12, 2 号仓(中间) = GPIO15, 3 号仓(出口白杆) = GPIO33。
static const uint8_t SERVO_PIN[3]  = {12, 15, 33};   // 舵机3=GPIO33; GPIO23 留给 HuskyLens I2C SCL (2026-11-10 用户定)
static const int     SERVO_REST_DEG  = 270; // 收回/安全位 (us=2500) — 上电归位推杆缩回, 不挡带面
static const int     SERVO_EJECT_DEG[3] = {-80, -80, -80}; // [1,2,3] 展开/出射角 (us=556) — 拨料时伸出推料
static const uint32_t SERVO_SWEEP_MS   = 3000; // 一次扫掠总时长 (拨出 1500 + 归位 1500)
static const uint32_t SERVO_ITEM_GAP_MS = 3500; // 件间间隔 (>= 扫掠时长)
// 停带推料窗口 (新方案: IR 定时到仓 -> 瞬停 -> 展开 -> hold -> 收回 -> 恢复):
//   展开 = 270->-80, 收回 = -80->270. hold=0 时无额外驻留 (在线 hold 可调).
static const uint32_t SERVO_EJECT_MOVE_MS   = 600;  // 270->-80 展开行程 (停带窗口内)
static const uint32_t SERVO_RETRACT_MOVE_MS = 900;  // -80->270 收回行程
// 手动推杆 jog (网页按钮 / 测试) 专用行程: 比真推短, 让「推杆中」按钮停留更短.
// 展开仍 < 真推 600ms (舵机仍能完全展开), 仅网页手动测试用, 不影响自动推杆.
static const uint32_t SERVO_JOG_EJECT_MS   = 400;  // 手动 jog 展开
static const uint32_t SERVO_JOG_RETRACT_MS = 600;  // 手动 jog 收回 (合计 ~1000ms)

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
    {6, 1, "pcb"},           // 自学习分类 id1 = 手扶 PCB (占位, 训练后确认 id/仓)
    {129, 0, "pcb"},        // 已安装定制模型 129 (>=ALGORITHM_CUSTOM_BEGIN=128), 类别0 = 手扶 PCB (2026-11-13 I2C 实测确认 id=0)
};
static const int MATERIAL_MAP_LEN = sizeof(MATERIAL_MAP) / sizeof(MATERIAL_MAP[0]);
