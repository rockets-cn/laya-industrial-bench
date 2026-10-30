# MQTT 分拣协议约定 (sort-mqtt)

3 个分拣器 + 1 条传送带 (line1) 的 MQTT 控制契约。真实 PLC/控制器只需实现
「下行命令 / 上行应答 / 遥测」三组 topic，即可被 `sort_mqtt.py` 网关直接接管；
无硬件时用 `sim_devices.py` 做闭环参照 (行为与本契约逐条对齐)。

```
 语音回路 loop_ui /run ──HTTP POST /dispatch──┐
 SCADA / 手工   ──MQTT sort/inbound/command──►│
 视觉传感器     ──MQTT sort/inbound/vision───► 网关 sort_mqtt.py (:8123)
                                                │  白名单校验 / 顺序执行 / 统计分发
                                                ▼
                              MQTT broker (dev: amqtt · prod: mosquitto)
                                                │
              ┌─────────────────────────────────┼──────────────────────────┐
              ▼                                 ▼                          ▼
   sort/line1/conveyor/*              sort/sorter1/cmd/ack/telemetry  sort/sorter{2,3}/...
              传送带                      分拣器 1                          分拣器 2/3
```

约定: **1 号料场 = 1 号分拣器**, N 号料场 = N 号分拣器 (yard 1..3)。

## 下行命令 (网关 → 设备, QoS1)

### 传送带 `sort/line1/conveyor/cmd`

```json
{"job_id": "j1732345-1", "action": "start", "speed_mps": 1.0, "ts": "..."}
```

| action | 目标态 | 说明 |
|---|---|---|
| `start` | running | 启动/恢复, 携带 `speed_mps`; 已 running 回 ok (只更新 speed) |
| `resume` | running | 恢复; 已 running 回 ok; stopped 时回 busy |
| `pause` | paused | 暂停 (网关故障安全时也会发); 已 paused 回 ok; stopped/fault 回 busy |
| `stop` | stopped | 停止; 已 stopped 也回 ok |
| `reset` | stopped | 故障复位 (人工介入后); 非故障 no-op ok |

**幂等目标态语义**: 设备已处于目标态时必须回 `ok`, 不得回 `busy` ——
网关把 `busy` 解释为「设备正在执行本 job」并延长等待, "已停" 回 busy 会让网关空等。
fault 期间 start/stop/pause 回 busy, 只有 `reset` 可走。

状态机: `stopped --start--> running --pause--> paused --resume--> running`;
`running/paused --stop--> stopped`; 故障 → `fault --reset--> stopped`。

### 分拣器 `sort/sorter{n}/cmd` (n = 1..3)

```json
{"job_id": "j1732345-1", "action": "divert", "material": "red_block",
 "qty": 5, "trigger": "photo", "ts": "..."}
```

| action | 说明 |
|---|---|
| `divert` | 从传送带分出 `qty` 件 `material` 到本分拣器料道 |
| `reset` | 故障复位到 ready; armed 时取消预置 (不推出, 回 ok "cancelled") |

字段:

- `trigger` (可选, 默认 `immediate`):
  - `immediate`: 收到命令立即开始推 (时序靠安装距离标定, 见「光门时序」节);
  - `photo`: **光门触发** — 设备进入 `armed` 等待, 本分拣器前侧光门被物品
    通过 (落沿) 才开始推; `qty>1` 时每件各等一次光门 (推完一件回 armed 等
    下一件)。光门等待窗 (固件 5s) 内无物品 → 对 `job_id` 回 `fault`
    `note="photo timeout"` (期望的件没到, 必须锁存)。**光门解析顺序**
    (固件): 物理光门 (MCP23017 PB0/1/2 已接) → **虚拟光门** (未装光门时,
    用相机检测锚点 + 带速匀速外推预测物品过光门线时刻, 见「光门时序」
    「虚拟光门」节) → `immediate` 回退 (串口日志说明原因)。

状态机:

```
ready --divert(immediate)--> busy --(动作完成)--> ready
ready --divert(photo)--> armed --(光门落沿, 一件)--> busy
busy --(photo 且未完件)--> armed   ← 每件各等一次光门
armed --reset--> ready (取消)      armed --(光门超时无物品)--> fault
fault --reset--> ready
```

- 分拣器 `state` 值: `ready | armed | busy | fault` (armed = 光门预置中)。
- `busy` 语义: 正在执行; `armed` 时收到新 divert 同样回 busy-ack。

## 上行应答 (设备 → 网关, QoS1, 按 job_id 关联)

topic: `sort/line1/conveyor/ack/{job_id}` / `sort/sorter{n}/ack/{job_id}`

```json
{"job_id": "j1732345-1", "action": "divert", "result": "ok", "note": "", "ts": "..."}
```

- `result`: `ok` (接受并执行) | `busy` (设备正在执行中) | `fault` (设备故障)。
- **三种应答, 三种网关行为**:
  - `ok` → 命令成功, 进入下一步;
  - `busy` → 设备正在做这件事: 网关**绝不重发** (重发 = 重复推出), 只延长等待
    `busy_wait_s` (默认 3s), 最多 `busy_wait_max_ext` (默认 3) 次, 迟到的 ok 仍入账;
  - `fault` → 立即失败, 不重试。
- **busy 只用于 "正在执行"**: 命令按幂等目标态语义, "已处于目标态" 必须回 `ok`。
  例如同 `job_id` 的 divert 重发/重投: 设备若已推完只补 `ok` 应答, 绝不重新推出
  (固件按 job_id 去重, 窗口 60s)。
- **时限**: divert 实际执行 ≈ 0.9s/件, ok 应答在 `qty` 件全推完才发, 故网关
  divert 超时 = `ack_timeout_s` (默认 3s) + `divert_timeout_per_item_s` (默认 1s) × `qty`;
  `trigger=photo` 时再加 `photo_margin_s` (默认 3s, 相机→光门行程预算);
  其他命令用 `ack_timeout_s`。整窗无应答 → 重发, 最多 `ack_retries` (默认 2) 次;
  仍失败 → **故障锁存**: 停止后续下发、暂停传送带, 需人工清除 (见下)。

## 遥测 (设备 → 网关, QoS0, 2 Hz)

-

topic: `sort/line1/conveyor/telemetry` / `sort/sorter{n}/telemetry`

```json
{"state": "running", "speed_mps": 1.0, "items_in": 42, "ts": "2026-10-30T12:00:00+08:00"}
{"state": "busy", "diverted_total": 17, "last_material": "red_block", "ts": "2026-10-30T12:00:01+08:00"}
```

- `state` 为必填 (网关状态机依赖); 其余为设备特有可选字段:
  - 传送带: `items_in` (过件累计, 有计数传感器时); 物理控制器 (edge101) 无过件
    计数, 发 `steps_total` (步进累计步数) —— 两者不同量纲, 契约均按可选对待;
  - 分拣器: `diverted_total`, `last_material`;
  - `ts`: ISO8601; 网关忽略该字段。物理控制器无 RTC (后续可 NTP), 可为 `""`。
- 传送带 `state`: `stopped|running|paused|fault`
- 分拣器 `state`: `ready|armed|busy|fault`
- 网关据此选择传送带动作 (running 跳过 / stopped→start / paused→resume /
  fault→直接故障等人工 reset)。遥测丢失不阻塞命令路径, 只影响状态优化。

### 运动事件 `sort/line1/motion` (设备 → 网关, QoS0, 状态迁移时)

分拣器每次状态迁移广播一条 — 「推杆何时推出 / 光门何时触发」的时间线,
供「让示意图动起来」的动画还原实际情况 (网关维护 `GET /motion` + relay
`sort/event/motion`, 见「网关事件与统计」):

```json
{"sorter": 1, "state": "busy", "via": "virtual", "job_id": "j1732-7", "ts": "",
 "item": 2, "of": 3}
```

- `via` = 迁移原因: `immediate` (收令即推) / `photo` (物理光门沿) /
  `virtual` (虚拟光门预测时刻到) / `timeout` (光门等待超时 fault) /
  `reset` (人工复位) / `done` (job 完成) / `next` (photo job 多件: 推完
  一件回 armed 等下一件) / `cancel` (armed 被 reset 取消)。
- `ts` 物理控制器无 RTC 可为 `""` (与遥测相同约定); 时间线间隔 = 相对时刻。
- `item`/`of` = 逐件件号 (1-indexed / 总件数): `qty>1` 时每件各触发一次光门
  + 一次推杆扫掠, 对应的事件序列 — `busy`/`timeout` = 正在处理(未到场)的
  第几件, `next`/`done` = 刚完成(刚拨出)的那件; 件数 `of` 即本 job 的 qty.
  旧固件/不适用事件 (reset/cancel) 缺省不带该字段 (网关记 0)。
  数字孪生据此逐件还原「带面行走 → 过相机 → 光门 → 拨入」的完整动作序列。

## 入站指令 (→ 网关)

### `sort/inbound/command` (QoS1) — SCADA / 手工

```json
{"source": "scada", "path": "MQTT", "job_id": "j1732345-1",
 "actions": [{"yard": 1, "material": "red_apple", "qty": 1},
             {"yard": 1, "material": "red_apple", "qty": 1},
             {"yard": 3, "material": "blue_block", "qty": 1}]}
```

- 动作逐条 (action = 1 件): 规则层把「三个香蕉进一号仓库」展开为 3 条
  `qty=1` 动作 — 逻辑视图 (页面动作列表 / 台账 / 批事件)。
- 指令合并: 网关下发 PLC 时同 料场+物料 的连续动作合并回一条
  `divert` 命令 (qty=3): 设备状态机按一条命令逐件执行 (每件一次光门
  落沿 + 一次推杆扫掠, motion 事件带件号 1/3, 2/3, 3/3); 同 job_id
  连发多条命令会被 busy-ack 折叠, 合并才是物理正确的下发方式。

### `sort/inbound/vision` (QoS1) — 视觉传感器

```json
{"material": "blue_block", "confidence": 0.97, "frame_id": "f-1732-7",
 "source": "hsv_block(4ms)", "bbox": [x, y, w, h], "ts": "..."}
```

- 白名单校验 (8 物料) + `confidence >= confidence_min` (默认 0.5, 见
  `config/material_map.json`) + 物料→分拣器映射 (`fixed` 表或 `round_robin`)。
- 未知物料 / 低置信 → 只计数统计, **不下发** (安全降级)。
- 每个事件 = 1 件物料 (qty 固定 1)。多帧重复上报的防抖策略见
  `OPEN_QUESTIONS.md` 第 4 条 (未决)。
- 坐标 `x/y/w/h` (传感器像素系, 固件 visionJson 已带): 网关**不下发**给设备,
  但随 `sort/event/batch` 的 `vision` 字段与 JSONL 台账留痕 — 上游标定
  (相机几何 → 物品在带上的实际位置) 与末端对账需要这份原始数据。
- `pos_mm` (可选): 物品在检测时刻的**带面位置** (mm, 相机参考系, + = 下游),
  由固件 `Track::pixelToMm` 从像素换算 (标定项 `VIEW_WIDTH_MM`, 见
  `edge101/include/config.h`); 同时是虚拟光门预测的输入。网关同样只留痕。

### `sort/human/fault_clear` (任意 payload)

人工故障清除。等价于 HTTP `POST /fault/clear`。

## 白名单

- 物料 (8): `red_apple 红苹果 / green_apple 绿苹果 / banana 香蕉 / pear 梨 /
  red_block 红块 / green_block 绿块 / yellow_block 黄块 / blue_block 蓝块`
  (完整值: `red_apple, green_apple, banana, pear, red_block, green_block,
  yellow_block, blue_block`)
- yard: `1..3`; qty: 整数 `1..99`; 队列深度 `queue_max` (默认 16, 超出丢弃并计数)。

## 网关事件与统计 (网关 → 订阅者)

| topic | QoS/retain | 内容 |
|---|---|---|
| `sort/event/batch` | 0 (fault/done 时 retain) | `{phase: accepted\|done\|fault, job_id, source, actions, vision, fault}` (`vision` = 视觉入站的物料/置信度/x/y/w/h/pos_mm, 非视觉批次为 null) |
| `sort/event/dispatch` | 0 | 每条 divert 成功一次: `{job_id, sorter, material, qty, ts}` |
| `sort/event/motion` | 0 | 设备运动事件的 relay (时间线源 = 设备 `sort/line1/motion`, 见「遥测」节): `{ts, sorter, state, via, job_id}` |
| `sort/event/fault` | 1, **retain** | 故障锁存详情 (人工清除前一直可见) |
| `sort/event/cleared` | 1, **retain** | 故障清除确认 |
| `sort/stats/summary` | 1, **retain**, 周期 5s | 统计汇总 (见下) |

### `sort/stats/summary` (统计分发)

```json
{
  "ts": "...", "uptime_s": 123.4,
  "batches": {"total": 12, "done": 11, "fault": 1, "reject": 0, "queue_dropped": 0},
  "items": {"dispatched": 96,
            "by_sorter": {"1": 40, "2": 30, "3": 26},
            "by_material": {"red_apple": 22, "blue_block": 30, "...": 0}},
  "cmd": {"sent": 44, "ack_ok": 42, "ack_timeout": 2, "retries": 2},
  "vision": {"events": 96, "dispatched": 90, "unknown": 3, "low_conf": 3},
  "latency_ms": {"cmd_to_ack": {"mean": 31, "p95": 58, "max": 120},
                 "batch_e2e": {"mean": 902, "max": 1500}},
  "throughput_items_per_min": 10.2,
  "fault": null
}
```

另有 **JSONL 台账** `stats/sort_ledger.jsonl`: 每 job 一行
(`ts, job_id, source, path, outcome, actions, fault, e2e_ms`), 供离线审计与对账。

## 光门时序 (推杆推出定位)

> 分拣器「何时推」的几何与标定。`divert_trigger=photo` (edge101 已实现)。
> **光门解析顺序**: 物理光门 (MCP23017 已接) → 虚拟光门 (未装光门时按带面
> 位置预测, 见下) → `immediate` 回退。**安装布局示意图**:
> `edge101/docs/gate_layout.svg` (也内嵌在 loop_ui「Laya 工业决策回路」页
> 「📐 分拣线光门安装布局」折叠区)。

### 触发光门 — 装【前侧 (上游)】, 不装推杆推料点处

推出动作 (舵机驱动推杆推出 400ms + 收回) 必须在**物品到达推杆推料点之前完成**
(推杆要提前伸出到位)。所以光门落沿只是「推出开始」的时刻, 不是「物品到位」
的时刻。安装公式:

```
光门安装位置 = 推杆推料点 − 带速 × (推出动作时间 + 余量)

例: 带速 1.0 m/s, 推出 400ms + 余量 100ms
   → 光门装在推杆推料点前方 0.5 m
```

物品过光门 → 固件起舵机 → 推杆 0.4s 推出到位, 物品 0.5s 后到达推料点,
留 0.1s 余量。现场标定: 放测试件跑, 打偏了前后挪安装支架 — 1m/s 下
**1cm = 10ms**, 用串口 `gates` 命令确认每路光门有沿。

- 每推杆 1 路光门 (edge101: MCP23017 PB0/PB1/PB2 → 1/2/3 号),
  透过式光电, NPN 开集 + 内部上拉 (无物品=HIGH, 挡光=LOW, 落沿=物品到达)。
- **件距约束**: 相邻两件间距 ≥ 带速 × (推出 400ms + 件间 500ms)
  (1m/s → ≥0.9m), 否则第二件到光门时第一件还没推完, 第二件漏推。
  上游 (Laya/SCADA 排产) 必须保证件距。
- 光门等待窗 (固件 5s) < 带速 × 最大件距 时, 慢件流会 photo 超时误锁存 —
  件距上限 = 5m @1m/s。

### 虚拟光门 (未装光门时: 带面位置预测)

光门硬件到货/接线前, `trigger=photo` 也能按示意图时序运行: 固件用
**相机检测锚点 + 带速匀速外推** 预测物品过光门线的时刻, 到点推出
(纯逻辑在 `edge101/lib/SortLogic/src/Track.h`, 主机可单测):

```
p(t) = p0 + v·(t − t0)
  p0 = 检测时刻物品带面位置 (mm, 相机参考系, 像素换算, 见「坐标标定」)
  t0 = HL2 检测时刻 (固件 millis)
  v  = 传送带当前 speed_mps (RMT 步进起停已知)
预测: t_gate = t0 + (GATE_DIST_MM[n] − p0) / v   (物品过 n 号光门线时刻)
```

- 命令到达时刻无关: 预测基于 t0 (检测时刻), 网络/网关延迟不影响触发时刻;
  匹配窗 3s (`TRACK_MATCH_WINDOW_MS`) 内同物料检测才成立, 且带速 > 0。
- 匹配失败 (无检测/带停/物品已过光门线) → 回退 immediate, 串口日志说明。
- `qty>1` 的件距节奏: 第 k 件在 `t_gate + (k−1)×ITEM_PITCH_MM/v`
  (固件 `config.h` `ITEM_PITCH_MM`, 默认 900mm @1m/s, 与件距约束一致)。
- 预测精度 = 带速误差 + 像素标定误差 + 物品在视野内的位置误差; 光门硬件
  到位后自动优先走物理光门 (虚拟光门只在 `gateOk=false` 时启用)。
- 调试: 串口 `track` 命令查看当前锚点/带速/标定值; 串口 `vision <material>`
  注入检测锚点 (p0=0, 视为物品在相机中心) 可离线验证虚拟光门时序。

### immediate 模式 (未装光门) 的等价标定

`divert_trigger=immediate` 时, 时序 = 相机识别延迟 + MQTT 往返 + 网关处理,
**总延迟是变量**。推杆安装距离 = 检测点 + 带速 × 实测总延迟 (实测
视觉链路 ~370ms @1m/s ≈ 37cm, 波动 ±几十ms → ±几cm)。对件宽有公差的
物料可用, 精度要求高时上 photo。

### 坐标标定 (x/y/w/h → 带面位置)

HL2 对象帧带像素坐标, 经 `sort/inbound/vision` 透传并由网关随批次事件/
台账留痕。像素 → 带面距离的换算需要相机几何标定:

```
带面 x 位置 ≈ (像素 X − 画面中心) / 画面宽度 × 视场宽度(FOV, 按安装高度算)
            + 相机相对光门的安装偏移
```

用途: (a) 把「物品实际位置」从「检测时刻」修正到「物品本身」,
减小 immediate 模式标定误差; (b) **虚拟光门**: 固件按
`p0 = (像素 X − 画面中心)/画面宽 × VIEW_WIDTH_MM` 换算带面位置
(随视觉事件以 `pos_mm` 透传), 预测物品过光门线时刻; (c) 未来编码器
闭环对位预留。标定方法: 在带面贴 N 个已知位置标记, 记录
(HL2 像素, 实际位置) 对, 线性拟合 → 更新 `VIEW_WIDTH_MM` /
`VIEW_CX_PX` (固件 `edge101/include/config.h`)。

### 区分: 后侧 (料道末端) 光电是「对账」, 不是时序

料道入口/末端的光电用于**确认件真的进道了** (长期计数对账,
OPEN_QUESTIONS 第 3 条), 与本次 3 路前侧触发光门职责不同, 互不替代。

## 安全降级 (与语音回路同一哲学)

1. 命令超时 × (retries+1) → 故障锁存 → **暂停传送带** → 暂停一切下发。
2. 恢复只能人工: `POST /fault/clear` 或 `sort/human/fault_clear`。
3. 传送带遥测 `fault` → 新 job 直接判故障, 等人工对设备 `reset`。
4. 视觉未知/低置信 → 静默计数, 绝不下发。
5. 队列满 → 丢弃 + `queue_dropped` 计数 (不阻塞)。

## HTTP 管理接口 (网关 :8123, MQTT 入站的等价入口)

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | broker 连接 / 队列 / 活动 job / 故障状态 |
| `GET /stats` | 同 `sort/stats/summary` |
| `GET /motion` | 运动事件时间线 (设备状态迁移, 最近 `motion_keep` 条): `{count, motion: [{ts, sorter, state, via, job_id}]}` — 「让示意图动起来」的动画数据源 |
| `POST /dispatch` | `{actions, source?, path?}` → 202 `{job_id, queued}` / 409 故障锁存 / 422 非法 |
| `POST /fault/clear` | 人工清除故障 |

## 配置

- `config/sort_gateway.json`: broker / 端口 / 分拣器数 / ack 超时 / 重试 /
  推杆推出触发方式 (`divert_trigger`: immediate\|photo) / 光门行程预算
  (`photo_margin_s`) / 运动事件时间线深度 (`motion_keep`, 默认 100) /
  统计周期 / 批间停带 (`stop_conveyor_after_batch`) /
  队列深度 / 台账路径。
- `config/material_map.json`: 物料→分拣器映射 (`fixed` 表或 `round_robin`) + 置信度下限。
  **当前映射是占位**, 待现场确认 (OPEN_QUESTIONS 第 1 条)。
- 环境变量: `SORT_MQTT_BROKER` (默认 `tcp://127.0.0.1:1883`)、
  `SORT_GATEWAY_PORT` (默认 8123)。

## 控制器 (PLC) 接入清单

1. MQTT 3.1.1 客户端, 连网关所在 broker; 实现 `sort/line1/conveyor/cmd` 与
   `sort/sorter{1,2,3}/cmd` 订阅, 按上表应答 + 2Hz 遥测。
2. 应答必须原样带回 `job_id` (网关按 (设备, job_id) 关联)。
3. 先跑通模拟器闭环:
   ```bash
   .venv/bin/python broker_local.py        # 终端 1
   .venv/bin/python sim_devices.py         # 终端 2 (行为参照)
   .venv/bin/python sort_mqtt.py           # 终端 3 (网关)
   .venv/bin/python tests/test_sort_mqtt.py  # E2E 52 项 (独立进程, 不需上面三个)
   ```
4. PLC 接入后, 用 `sim_devices.py --fault-rate/--no-ack` 的行为当故障测试参照。
5. 认证/TLS: 当前网关 paho 客户端**未配置**用户名/密码/TLS — 若生产 broker 需要,
   见 OPEN_QUESTIONS 第 6 条。
