# edge101 — 分拣控制器固件 (EDGE 101 / DFR1283)

EDGE 101 (DFRobot, **ESP32 原版** — 非 S3; 板载 RJ45 以太网 + 4G CAT1)
实现的物理分拣控制器, 行为与 `sim_devices.py` 等价, 直接实现
`docs/sort-mqtt.md` 协议 → 对网关 `sort_mqtt.py` 零改动。

```
HuskyLens 2 (UART, GPIO5/12 @38400) ─┐
                                     ▼
              EDGE 101  ◄── 步进 (传送带, STEP=14 DIR=15)
                     │      3 舵机 (PCA9685 I2C @0x40, SDA=18 SCL=23)
                     │  MQTT: sort/line1/conveyor/*  sort/sorter{1,2,3}/*
                     │        sort/inbound/vision   (板载 RJ45, 静态 192.168.2.2)
                     ▼
            broker (192.168.2.1:1883)
```

## 目录

```
edge101/
├── platformio.ini            # 构建配置 (env: edge101, ESP32 原版, 16MB flash)
├── include/config.h          # 引脚/网络/参数 (依据 DFRobot wiki DFR1283)
├── src/main.cpp              # 固件主程序
├── lib/SortLogic/src/
│   ├── Conveyor.h            # 传送带状态机 (纯 C++, 无 Arduino 依赖)
│   ├── Sorter.h              # 分拣器状态机 (纯 C++)
│   ├── HuskyLens.h           # HL2 UART 帧解析 + 进画面去重 (纯 C++)
│   └── Proto.h               # 协议 JSON 构造 (纯 C++)
├── lib/ServoI2C/src/
│   └── ServoI2C.h            # PCA9685 16ch I2C 舵机驱动 (50Hz PWM)
├── tests/host/
│   ├── test_logic.cpp        # 主机单测 (状态机对拍/帧解析/去重/JSON)
│   └── run_tests.sh          # g++ 一键编译+运行
└── docs/wiring.md            # 接线图 (引脚依据 wiki 实测规格)
```

## 构建

```bash
# ESP32 固件 (首次自动下载 espressif32 平台 + xtensa 工具链, ~500MB)
pio run
pio run -t upload          # 烧录 (Type-C USB; 必要时按 BOOT 进下载模式)
pio device monitor         # 115200 串口监视

# 主机单测 (验证纯逻辑, 无需硬件/无需 PIO)
./tests/host/run_tests.sh  # 42 项: Conveyor/Sorter 状态机, HL2 帧+去重, 协议 JSON
```

> `pio` 不在 PATH 时: 项目 venv 里已装 PlatformIO 6.2.0,
> 用 `<repo>/.venv/bin/pio` 代替。

## 引脚分配 (板子只有 6 路可用输出)

| 信号 | 引脚 | 备注 |
|---|---|---|
| HL2 TX/RX | GPIO5 / GPIO12 | Serial2 重映射, 38400 |
| 步进 STEP/DIR | GPIO14 / GPIO15 | DIR 复用板载 LED (DIR=LOW→亮) |
| I2C SDA/SCL | GPIO18 / GPIO23 | PCA9685 @0x40, 通道 0/1/2 = 1/2/3 号舵机 |
| 步进 EN | — | 驱动器端接 5V 常使能 (无空闲 GPIO) |
| 以太网 | 板载 RJ45 | MDIO PHY 自动识别, 静态 IP |

P34/P37/P38/P39 只能输入 (P38=板载按钮), P33/P34 被 4G 模组占用。

## 行为要点 (与 sim_devices 对拍)

- **传送带**: `start/resume/pause/stop/reset` 状态机 + 步进脉冲 (非阻塞,
  `speed_mps × STEPS_PER_MM` 换算步频); busy 时回 `busy` ack。
- **分拣器**: `divert` 进 busy, PCA9685 每次完成一件 `stepOne()`, N 件拨完才回
  `ok` ack; `reset` 清 fault; fault 时任何 divert 回 `fault` ack。
- **视觉**: HL2 对象帧 (type=0x01) 解析 → `{object_type, ID} → material`
  映射表 (config.h `MATERIAL_MAP`, 可换) → 进画面去重 (同物料 500ms 滑动窗,
  离屏后重入才再报) → 发 `sort/inbound/vision`。
- **遥测**: 2Hz 发 conveyor + 3×sorter 状态 (网关 5s 无遥测判离线)。

## 串口控制台 (115200, 换行结尾)

```
help                 # 列出命令
state                # 打印 conveyor/sorters/diverted_total/steps
stepper <n>          # 手动 n 步 (验证转向)
sweep <n>            # 手动扫掠 n 号舵机一次
vision <material>    # 手动注入一条视觉事件 (联调用)
```

## 物料映射 (MATERIAL_MAP)

`{object_type, ID, material}` — 当前占位:
1..4 号色块=red/green/blue/yellow_block, 自定义模型 ID10=banana。
水果/新物料: HuskyLens 2 训练或导入自定义模型 (YOLO/基础分类),
新模型输出的 (type, ID) 加一行即可, 协议层不动。
物料名必须与网关白名单 (`config/sort_gateway.json`) 一致。

## 与 DFRobot 官方 SDK 的关系

wiki 提供 Arduino IDE 板包 (package_Edge101_index.json, 板名
"Edge101 IOT Controller")。本固件用 PlatformIO 的通用 `esp32dev`
+ 16MB flash 构建, 依赖的核心库 (`ETH.h` / `PubSubClient` / ArduinoJson)
与官方 SDK 一致; 若烧录遇到问题, 可对照官方板包的 flash/PSRAM 参数微调
`platformio.ini`。
