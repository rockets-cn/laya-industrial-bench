# 分拣线接入 · 未决问题 (待讨论)

> 2026-10-29 夜。以下为自动推进时**没有结论、需要拍板**的部分；
> 已实现内容见 `docs/sort-mqtt.md`、`edge101/` 与下方「本次已完成」。

## 本次已完成 (过夜自动推进)

| 项 | 说明 |
|---|---|
| `vision_input.py` | 本机视觉接入 (HSV 4 色块): 帧 → 发 `sort/inbound/vision`。外部带模型视觉传感器 (含 HuskyLens 2) 可跳过本模块直发同一 topic |
| `tests/test_sort_mqtt.py` | 网关 E2E 31 项全通过 (进程内 amqtt broker + 模拟设备 + 网关) |
| `sort_mqtt.py` 修复 ×3 | ① paho 2.x 非法通配符致订阅线程崩溃 ② 遥测正则不匹配裸 topic ③ paused 后按遥测 start/resume |
| `loop_ui` 下发按钮 | 「📤 下发分拣线」: Laya 分类结果 → `POST /dispatch` → 网关 :8123 (手动触发, 不自动) |
| `docs/sort-mqtt.md` | 控制器接入协议契约 (命令/应答/遥测/白名单/统计/安全降级) |
| **EDGE 101 固件** `edge101/` | 按 DFRobot wiki DFR1283 实测规格实现: 板载 RJ45 静态 IP + HL2 UART (GPIO5/12 重映射 Serial2) + 步进 (STEP=14 DIR=15) + PCA9685 I2C 3 舵机 (SDA=18 SCL=23) + 2Hz 遥测; 行为与 sim_devices 等价, 对网关零改动 |
| 固件主机单测 | `edge101/tests/host/` 42 项全绿: Conveyor/Sorter 状态机对拍、HL2 帧解析 (断片/坏校验/垃圾字节恢复)、进画面去重 (500ms 滑动窗)、协议 JSON/topic |
| 实机闭环验证 | 全链路 (broker+模拟设备+网关+视觉) 手工跑通; 视觉 371ms, 语音 168ms, 7 命令 7 应答 0 超时 |
| 实测踩坑记录 | ESP32 UART 解析器两个 bug (非 0x55 数据字节被误当失同步 / 校验和越界读) 已修并以单测锁定; busy/fault 应答语义与 sim 对齐 (busy 也发 ack) |

## 未决 (需要结论)

### 物料与识别

1. **物料 → 分拣器映射** (原问题, 现在有两处要同步改)
   - 网关侧 `config/material_map.json` (语音路径 yard 判定 + 视觉路径料道选择)
   - 固件侧 `edge101/include/config.h` `MATERIAL_MAP` (HL2 {type,ID} → 物料名)
   现场每个料道实际放什么物料? 还是 round_robin? 当前两边都是占位。
2. **水果识别模型** (用户: "后续交给 huskylens2, 还没有想好")
   协议已就绪 (type=5 自定义模型 ID → 物料名), 剩下要定:
   用 HL2 自带基础分类还是导入 YOLO? 水果类别表? 训练样本谁出?
   *影响: 水果能否进视觉自动分拣闭环; 不定的话水果只走语音路径。*
3. **步进电机无编码器反馈**
   脉冲计数长期可能失步漂移 (堵转/打滑不自知)。要不要在料道末端加
   光电开关做对账 (超阈值报警)? *影响: 统计可信度。*

### 硬件采购与接线 (新, 固件按以下假设写的, 不符就改 config.h)

4. **舵机 ×3**: 假设 0–90° 扫掠、单次拨一件、动作时间 <400ms。
   实际舵机型号/扭矩/时间? 摆臂机构的几何 (90° 够不够扫到料道)?
5. **PCA9685 模块**: 固件按 I2C 0x40、50Hz、OUT0/1/2 写的。
   要采购 1 个 (板子只剩 6 路可用输出, 3 路舵机必须走 I2C)。
   模块 VDDIO 跳线拨 3.3V, 动力 5V ≥2A 独立供电。
6. **步进驱动器与电机**: 假设 A4988/DRV8825 三线 (STEP/DIR/EN),
   1/16 细分 (1600 步/米), 12V/1.5A+ 驱动。电机型号? EN 端接 5V 常使能
   (停带时线圈仍通电发热, 可接受吗? 还是加继电器切 EN?)
7. **16MB Flash 确认**: 固件按 16MB flash 构建。板上芯片若实际是 4MB,
   改 `platformio.ini` 的 `board_build.flash_size` (固件体积远小于 4MB, 不影响)。
8. **HL2 供电与电平**: 板 5V 直接带 HL2 (峰值 ~1.5W), TX 5V→3.3V 分压。
   如果 HL2 是 3.3V 供电版则更简单。
9. **4G 模组**: 不插 SIM 就不启用 (静默)。介意静态功耗 (~50mA) 吗?
   还是保留备用? (固件没碰 4G, 不影响。)

### 策略与运维 (沿袭, 未变)

10. **自动下发开关**: 保持手动「📤 下发」(防语音误识别直接落物理分拣)。
    要不要验证通过后自动下发? *建议: 至少加二次确认。*
11. **批间停带**: 当前 `stop_conveyor_after_batch: true`。连续生产要常开吗?
12. **统计留存**: 内存 + JSONL 台账 + retain summary 够吗? 转 SQLite/InfluxDB?
13. **service 化**: broker/sim/gateway 手动起。加 systemd 用户服务?
    上实体设备后 sim_devices 去掉, 只留 broker + gateway (+ vision 可选)。
14. **broker 认证**: 局域网 + 静态 IP 暂不需要; 若 broker 挪到公网/生产机,
    paho 客户端 (网关/固件) 要加 username/password/TLS — 固件里是硬编码
    匿名连接, 到时候一并改。

## 明早讨论入口

- 实体闭环: 按 `edge101/docs/wiring.md` 接线 → `pio run -t upload` →
  串口 `state` / `sweep` / `stepper` 自测 → 网关 `POST /dispatch` 全链路。
- 固件编译: ESP32 交叉编译已跑通 (pio run SUCCESS, Flash 668KB/1.25MB,
  RAM 34KB/320KB, 2026-10-29); 主机单测
  `./edge101/tests/host/run_tests.sh` 42/42 全绿。
