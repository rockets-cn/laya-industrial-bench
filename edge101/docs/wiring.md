# EDGE 101 接线图 (依据 DFRobot wiki DFR1283 实测规格)

> 引脚以 `include/config.h` 为准, 本表与固件一致。
> **板级事实**: ESP32 原版 (非 S3) · 16MB Flash · 板载 RJ45 以太网 ·
> 11 路原生 GPIO, 其中 4 路 (P34/P37/P38/P39) 只能输入 →
> **可用输出 6 路: GPIO5 / 12 / 14 / 15 / 18 / 23**。

## 引脚总表

| 信号 | 引脚 | 说明 |
|---|---|---|
| HL2 UART TX | GPIO5 | 板→HL2 RX (Serial2 重映射, 38400) |
| HL2 UART RX | GPIO12 | 板←HL2 TX (内部下拉, 仅作 RX 无影响) |
| 步进 STEP | GPIO14 | 步脉冲 (固件非阻塞输出) |
| 步进 DIR | GPIO15 | 方向; **复用板载 LED** (DIR=LOW 时 LED 亮, 可当方向指示) |
| I2C SDA | GPIO18 | 板载默认 I2C, 接 PCA9685 |
| I2C SCL | GPIO23 | 板载默认 I2C, 接 PCA9685 |

不可用 (已占用/只读): P33/P34 (4G 模组 PCIe UART), P37/P38/P39 (只读输入;
P38=板载按钮), 34/37/38/39 无上拉。

## 外设接线

### HuskyLens 2 (基础版, Gravity I2C/UART 口 → 用 UART)

| 线 | 接 | 说明 |
|---|---|---|
| 5V | 板 5V | HL2 峰值 ~1.5W, 板载 5V (USB 5V/2A 输入) 可带 |
| GND | GND | 共地 |
| TX | GPIO12 | 交叉: HL2 TX → 板 RX |
| RX | GPIO5 | 板 TX → HL2 RX |

- 波特率 **38400** (HL2 默认)。
- HL2 的 TX 是 5V 电平进 3.3V IO: 在 HL2 TX → 板 GPIO12 之间加
  1kΩ+2kΩ 分压, 或 3.3V 电平位移 (板侧有内部下拉, 低电平安全)。

### 步进电机 (传送带: 42 两相 + MKS SERVO42C 闭环 PCBA)

驱动器 = **Makerbase MKS SERVO42C** (闭环 PCBA, 贴电机尾部):
板载 **16384 线磁编码器** + FOC 矢量三环, 开环/矢量闭环/串口控制三模式,
堵转保护, 单圈内上电自动回零。安装/接线/参数参考:
https://blog.csdn.net/gjy_skyblue/article/details/120565771

| 线 | 驱动器 | 接 |
|---|---|---|
| PUL+ | STEP | GPIO14 |
| DIR+ | DIR | GPIO15 |
| EN+  | EN  | **MCP23017 PA0**（主）; 未接 MCP23017 时回退: 驱动器端跳线 5V 常使能 |
| GND  | GND | 共地 |
| 电机动力 | 12V | 12V/1.5A+ 独立供电 (不并 5V 轨; 实际电压按电机铭牌) |

- **EN 由 MCP23017 接管** (I2C 数字 IO 扩展, 与 PCA9685 共总线): 固件自动检测,
  在线时 running=使能、stopped/paused/fault=禁能 (停带断线圈电); 离线时行为
  同跳线 5V。详见下文 MCP23017 节。

- **工作模式: 矢量闭环 (STEP/DIR)** — 编码器兜底, 堵转/丢步由驱动器处理
  (可启用堵转保护, LED 状态指示)。开环模式亦可; **串口控制模式暂不用**
  (需要第三路 UART, EDGE 101 无空闲)。
- **安装注意** (文档第 2 部分): 磁环 (6×2.5) 必须粘在电机轴**中间**且水平;
  4 线顺序 A+ A- B+ B-, 插错上电屏幕提示 "Phase Line Error!", 断电重调。
- **细分**: 在驱动器上设 (屏幕/串口, 1~256 任意)。设 1/16 时固件
  `STEPS_PER_MM=320` (1.8° + 导程 10mm 假设); **改细分/换导程必须同步改
  config.h**, 否则带速不对。
- 1.0 m/s ≈ 320 脉冲/秒 (按上面假设)。
- 位置对账: 驱动器内可开"单圈内上电自动回零"; 要不要料道末端加光电开关
  做长期对账, 见 OPEN_QUESTIONS 第 3 项。
- **电机侧 6 线全部终结在驱动器, 不碰板子**: 线圈 4 线 A+ A- B+ B- →
  驱动器 A+/A-/B+/B- 端子; 动力 2 线 12V (V+/GND) → 驱动器动力端子。
  板子只出控制侧 STEP/DIR/GND 三根。

### PCA9685 I2C 舵机控制器 (3 路分拣舵机)

| 线 | 接 | 说明 |
|---|---|---|
| SDA | GPIO18 | 400kHz |
| SCL | GPIO23 | 400kHz |
| GND | GND | 共地 |
| VDD (逻辑) | 3V3 | 模块有 VDDIO 跳线则拨 3.3V |
| V+ (电机) | 5V 独立 | 舵机动力电源 (≥2A), **不要**用 I2C 3.3V |

### MCP23017 I2C 数字 IO 扩展 (步进 EN + 预留数字量, DFR0626)

与 PCA9685 **同一条 I2C 总线** (P18/P23), 地址不冲突: PCA9685 @0x40,
MCP23017 @0x27。参考: https://wiki.dfrobot.com.cn/_SKU_DFR0626_MCP23017_IIC_to_16_digital_IO_expansion_module

| 线 | 接 | 说明 |
|---|---|---|
| VCC | 5V | 接口供电 3.3~5.5V, 输出电平跟随 VCC |
| GND | GND | 共地 |
| C (SCL) | GPIO23 | 与 PCA9685 共线 |
| D (SDA) | GPIO18 | 与 PCA9685 共线 |
| A0/A1/A2 | 拨码全 1 | I2C 地址 0x27 (默认值) |
| PA0 | 驱动器 EN+ | 高电平使能 (A4988/DRV8825 惯例; 若 SERVO42C 极性相反, 改 `McpEn.h kActiveLow`) |

- **固件自动检测**: 启动日志 `[mcp] MCP23017 @0x27 在线, 步进 EN -> PA0
  (停带禁能)` 或 `[mcp] MCP23017 未检测到, 步进 EN 保持外部跳线 (5V 常使能)`。
- EN 随传送带状态: `running`=使能, `stopped/paused/fault`=禁能 (停带断线圈电,
  替代原「EN 跳 5V 常使能」); 串口 `stepper <n>` 手动脉冲时临时使能、发完恢复。
- **剩余 15 路预留** (PA1~PA7, PB0~PB7): 料道末端光电/限位/急停等数字量,
  输入模式 + 内部上拉即可 (OPEN_QUESTIONS 3②)。

- 3 个舵机信号线接 PCA9685 的 **OUT0/OUT1/OUT2** (通道 0/1/2 = 1/2/3 号料道)。
- 扫掠: 0°(归位) ↔ 90°(拨出), 一件 = 一次扫掠, qty=N 连拨 N 次。
- 地址默认 0x40 (若模块地址不同, 改 config.h `SERVO_ADDR`)。

### 以太网 (板载 RJ45, 网线直连)

- 无需外接模块。PHY=**IP101** (板内 MDIO 走线: MDC=GPIO4/MDIO=GPIO13/
  PWR=GPIO2/参考时钟=GPIO0, 参数出自 DFRobot 官方 SDK, 不占扩展口)。
- 固件 `ETH.begin(ETH_PHY_TLK110, 1, 4, 13, 2, ETH_CLOCK_GPIO0_IN)` 后
  停 DHCP 设静态 192.168.2.2/24 (config.h 可改)。
- 网线: EDGE101 ↔ 本机 (192.168.2.1/24, broker :1883)。
- **注意**: 本机直连若不开 DHCP/共享, 静态 IP 方案直接可用 (我们就是静态);
  wiki 演示的「电脑共享网络」场景可跳过。

## 供电架构

```
USB 5V/2A (或 DC 9-26V 端子)
  └── EDGE101 板载 (主控 + 4G 模组 + HL2)   ~1W 静态 + HL2 峰值
12V/1.5A+ 独立电源 ── 步进驱动器 VMOT
5V/2A+ 独立电源  ─── 舵机 (PCA9685 V+)   [与 12V 共用 GND 共地]
```

- 三处 GND 必须共地 (星形接地更好)。
- 若只有一个电源: DC 端子 9-26V + 板载 5V 带 HL2 + 外接 LDO/Buck 出 12V/5V 两路。

## 上电顺序与调试

1. 先不接电机/舵机, 只接 HL2: 串口 (115200, Type-C) 看
   `[hl2] ...` / `[eth] ...` / `[mqtt] connected`。
2. 接 PCA9685 + 一个舵机到 OUT0, 串口发 `sweep 1` 验证扫掠方向与角度。
3. 接步进, 串口发 `stepper 1000` (≈0.62m @1600步/米) 验证转向
   (DIR=LOW 时 LED 亮 = 正转, 反过来就把 DIR 反相接线)。
4. 全接上, 网关 `POST /dispatch` 走一次完整闭环。

## 调试命令 (串口控制台)

```
help | state | stepper <n> | sweep <n> | vision <material>
```
