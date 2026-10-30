# edge101 — 分拣控制器固件 (EDGE 101 / DFR1283)

EDGE 101 (DFRobot, **ESP32 原版** — 非 S3; 板载 RJ45 以太网 + 4G CAT1)
实现的物理分拣控制器, 行为与 `sim_devices.py` 等价, 直接实现
`docs/sort-mqtt.md` 协议 → 对网关 `sort_mqtt.py` 零改动。

```
HuskyLens 2 (UART, GPIO5/12 @38400) ─┐
                                     ▼
              EDGE 101  ◄── 步进 (传送带, STEP=14 DIR=15, EN=MCP23017 PA0, RMT 硬件脉冲)
                     │      3 舵机 (PCA9685 I2C @0x40, SDA=18 SCL=23)
                     │      MCP23017 I2C @0x27 (步进 EN + PB0/1/2 光门×3, 余 13 路预留)
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
│   ├── Proto.h               # 协议 JSON 构造 (纯 C++)
│   └── McpEn.h               # MCP23017 步进 EN (纯 C++, I2C 传输可注入)
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
./tests/host/run_tests.sh  # 104 项: Conveyor/Sorter 状态机 (含光门 photo 全路径),
                           # HL2 帧+去重, 协议 JSON, MCP23017 EN, 光门边沿去抖
```

构建已验证 (2026-10-30): `pio run` SUCCESS,
Flash 682KB/1.25MB (52%), RAM 35KB/320KB (10.7%), 无告警 (含 RMT 步进)。

实机以太网踩坑记录 (2026-10-29, 全部已修):

| 现象 | 根因 | 修复 |
|---|---|---|
| boot 后 `192.168.2.2` 不响应 | 此版本 arduino-esp32 3.x 默认 eth 的 netif if_key 是 **`ETH_DEF`** (非 `eth0`), `esp_netif_get_handle_from_ifkey("eth0")` 永远查不到, 静态 IP 从未配上 | `findEthNetif()` 先查 `ETH_DEF` 再回退 `eth0`; `tryStaticIp()` 幂等, setup 等 15s + loop 每 2s 重试 |
| ping 通但 MQTT 连不上 | PC 侧 UFW `policy drop` 默认拒入站, 1883/tcp 未放行 (ICMP 有默认限速放行所以 ping 能通) | `sudo ufw allow 1883/tcp` |
| PC 静态 IP 反复丢失 | NetworkManager 接管 enp2s0 做 DHCP, 插拔网线即清掉手加地址 | `nmcli con modify "Wired connection 1" ipv4.method manual ipv4.addresses 192.168.2.1/24` (持久) |

物理全链路验证 (2026-10-29): 直连网线 PC↔EDGE101, `POST /dispatch`
(yard=1, red_block, qty=2) → 网关批次 done, 13/13 命令 ack 0 超时;
板端 `state` → `diverted=2/0/0, steps=6212` (步进脉冲真实产生),
`eth` 命令确认 `up=1 ip=192.168.2.2`。串口诊断命令: `eth` / `mqtt`。

> `pio` 不在 PATH 时: 项目 venv 里已装 PlatformIO 6.2.0,
> 用 `<repo>/.venv/bin/pio` 代替。

## 浏览器仿真 (ESP-EMU)

[ESP-EMU](https://espressif.github.io/esp-emulator/) (v0.51) 只仿真
C3/C5/C6/H2 (RV32)、P4、S3 — **不支持原版 ESP32 (LX6)**, 所以 `edge101`
环境的 `firmware.bin` 装不进去。本工程另备 `s3emu` 环境 (ESP32-S3 目标,
以太网段 `#ifdef CONFIG_IDF_TARGET_ESP32` 跳过), 用于无硬件时验证
启动流程 / 串口控制台 / 状态机 / 步进脉冲:

```bash
pio run -e s3emu        # 产物 .pio/build/s3emu/firmware.bin
```

步骤:
1. 新标签页打开 https://espressif.github.io/esp-emulator/
2. 顶部 **Firmware** 载入 `edge101/.pio/build/s3emu/firmware.bin`
3. 右侧 **Speed** 调到 50~100 MIPS (或 live)
4. **UART0 console** 输入 (每行回车), 预期:
   - boot 日志: `[edge101] boot` … `[servo]` … `[mcp] MCP23017 未检测到…`
     `[hl2] UART2 @38400` … `[eth] 非 ESP32 原版目标…` `[edge101] ready`
   - `help` → 5 命令列表
   - `state` → `conveyor=stopped s1=ready s2=ready s3=ready diverted=0/0/0 steps=0`
   - `stepper 100` → `stepper 100 done` (GPIO 面板可见 STEP=GPIO14 翻转)
   - `sweep 1` → `sweep 1 done` (PCA9685 未接, I2C 写 NAK 不崩)
   - `vision 红色` → `vision 红色 published` (无 broker, publish 静默)
5. MQTT/以太网不参与仿真 (无网络硬件); 网关侧 E2E 与真机验证已覆盖该部分。

## 引脚分配 (板子只有 6 路可用输出)

| 信号 | 引脚 | 备注 |
|---|---|---|
| HL2 TX/RX | GPIO5 / GPIO12 | Serial2 重映射, 38400 |
| 步进 STEP/DIR | GPIO14 / GPIO15 | DIR 复用板载 LED (DIR=LOW→亮) |
| I2C SDA/SCL | GPIO18 / GPIO23 | PCA9685 @0x40, 通道 0/1/2 = 1/2/3 号舵机 |
| 步进 EN | MCP23017 PA0 | I2C @0x27 自动检测; 未接时回退驱动器端跳 5V 常使能 |
| 以太网 | 板载 RJ45 | MDIO PHY 自动识别, 静态 IP |

P34/P37/P38/P39 只能输入 (P38=板载按钮), P33/P34 被 4G 模组占用。

## 行为要点 (与 sim_devices 对拍)

- **传送带**: `start/resume/pause/stop/reset` **幂等目标态**状态机
  (已处于目标态回 `ok` 不回 busy — 网关把 busy 当"正在执行"会延长等待);
  步进脉冲由 **RMT TX 通道硬件产生** (1 个 [pulse,gap] 符号 × 无限循环,
  APB 80MHz 分辨率 → 320k pps 级带速; 主循环只在 start/stop 时
  `rmt_transmit`/`rmt_disable`), RMT 初始化失败自动回退主循环打脉冲 (~3kHz)。
  注意 ESP32 经典要求 `mem_block_symbols` 为偶数且 **>=64** (传 4 会
  ESP_ERR_INVALID_ARG 静默回退); 失败原因见 boot 日志 `[step] RMT 初始化
  失败 (channel=X encoder=Y)`, 运行时可用 `stepstat`/`stepinit` 诊断恢复。
  真机已验证: 1.0 m/s 下 320000 pps 硬件脉冲, 步数账实一致。
- **分拣器**: `divert` 进 busy, PCA9685 每次完成一件 `stepOne()`, N 件拨完才回
  `ok` ack; `reset` 清 fault; fault 时任何 divert 回 `fault` ack。
  **job_id 去重**: 完成窗 (60s) 内同 job_id 的 divert 重发/重投只补 `ok` 应答
  不重新拨料 (防网关重发/QoS1 重投导致双拨)。
  **光门触发** (`trigger=photo`): divert 先进 `armed` 预置, 推杆**前侧 (上游)**
  光门 (MCP23017 PB0/1/2, 透过式 NPN 开集 + 内部上拉, 每 loop 轮询, 10ms 去抖)
  被物品通过 (落沿) 才推; qty>1 每件推完回 `armed` 各等一次光门;
  5s 无物品 → fault "photo timeout" + 对 job_id 回 fault ack; 光门未接线
  (MCP23017 未在线) 时自动回退 immediate (启动日志说明)。安装公式:
  光门 = 推杆推料点 − 带速×(推出动作时间+余量); 布局示意图
  `docs/gate_layout.svg`; 详见 `docs/wiring.md` 光门节与 `docs/sort-mqtt.md`「光门时序」。
- **视觉**: HL2 对象帧 (type=0x01) 解析 → `{object_type, ID} → material`
  映射表 (config.h `MATERIAL_MAP`, 可换) → 进画面去重 (同物料 500ms 滑动窗,
  离屏后重入才再报) → 发 `sort/inbound/vision`。
- **遥测**: 2Hz 发 conveyor + 3×sorter 状态 (网关 5s 无遥测判离线)。
- **步进 EN**: MCP23017 PA0 接管 (上电自动检测): running=使能,
  stopped/paused/fault=禁能 (停带断线圈电); 未接 MCP23017 时回退 EN 跳线 5V。

## 串口控制台 (115200, 换行结尾)

```
help                 # 列出命令
state                # 打印 conveyor/sorters/diverted_total/steps
gates                # 3 路光门: ok (MCP 在线) + s1/s2/s3 (1=通光, 0=挡光)
stepstat             # RMT 状态: ready/failed/active/pps/steps_live
stepinit             # boot 时 RMT 创建失败后, 运行时重试并恢复
reboot               # 软重启 (esp_restart)
stepper <n>          # 手动 n 步 (验证转向)
sweep <n>            # 手动扫掠 n 号舵机一次 (驱动 n 号推杆推出)
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
+ 16MB flash 构建, 与官方 SDK 的差异已对齐:

- **PHY 参数**: 官方 SDK 实测 PHY=IP101 (TLK110 驱动), 地址 1,
  MDC=GPIO4, MDIO=GPIO13, PWR=GPIO2, 50MHz 参考时钟=GPIO0 —
  全部板内走线, 固件按此 6 参 `ETH.begin()` 启动, 不用 W5500 SPI 模块。
- **静态 IP**: 该版本 arduino-esp32 无静态 IP begin() 重载, 固件在
  `ETH.begin()` 后取 `eth0` netif, `esp_netif_dhcpc_stop` +
  `esp_netif_set_ip_info` 设 192.168.2.2/24 (config.h 可改)。
- 依赖库与官方一致: `ETH.h` (框架) + PubSubClient + ArduinoJson
  (本地 vendored: 注册表 nickpurdal/PubSubClient 已 404)。
