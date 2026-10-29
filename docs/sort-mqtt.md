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

| action | 前置状态 | 说明 |
|---|---|---|
| `start` | stopped | 启动, 携带 `speed_mps` |
| `resume` | paused |  | 恢复, 携带 `speed_mps` |
| `pause` | running | 暂停 (网关故障安全时也会发) |
| `stop` | running/paused | 停止 |
| `reset` | fault | 故障复位到 stopped (人工介入后) |

状态机: `stopped --start--> running --pause--> paused --resume--> running`;
`running/paused --stop--> stopped`; 故障注入 → `fault --reset--> stopped`。

### 分拣器 `sort/sorter{n}/cmd` (n = 1..3)

```json
{"job_id": "j1732345-1", "action": "divert", "material": "red_block",
 "qty": 5, "ts": "..."}
```

| action | 说明 |
|---|---|
| `divert` | 从传送带分出 `qty` 件 `material` 到本分拣器料道 |
| `reset` | 故障复位到 ready |

状态机: `ready --divert--> busy --(动作完成)--> ready`; 故障 → `fault --reset--> ready`。

## 上行应答 (设备 → 网关, QoS1, 按 job_id 关联)

topic: `sort/line1/conveyor/ack/{job_id}` / `sort/sorter{n}/ack/{job_id}`

```json
{"job_id": "j1732345-1", "action": "divert", "result": "ok", "note": "", "ts": "..."}
```

- `result`: `ok` (接受并执行) | `busy` (当前状态不允许该动作) | `fault` (设备故障)。
- 时限: 网关在 `ack_timeout_s` (默认 3s) 内未收到应答视为超时, 重试
  `ack_retries` (默认 2) 次; 仍失败 → **故障锁存**: 停止后续下发、暂停传送带,
  需人工清除 (见下)。
- 网关把 `busy`/`fault` 都当失败处理 (状态失步需要人工确认, 不自动重试状态)。

## 遥测 (设备 → 网关, QoS0, 2 Hz)

-

topic: `sort/line1/conveyor/telemetry` / `sort/sorter{n}/telemetry`

```json
{"state": "running", "speed_mps": 1.0, "items_in": 42, "ts": "..."}
{"state": "busy", "diverted_total": 17, "last_material": "red_block", "ts": "..."}
```

- 传送带 `state`: `stopped|running|paused|fault`
- 分拣器 `state`: `ready|busy|fault`
- 网关据此选择传送带动作 (running 跳过 / stopped→start / paused→resume /
  fault→直接故障等人工 reset)。遥测丢失不阻塞命令路径, 只影响状态优化。

## 入站指令 (→ 网关)

### `sort/inbound/command` (QoS1) — SCADA / 手工

```json
{"source": "scada", "path": "MQTT",
 "actions": [{"yard": 1, "material": "red_apple", "qty": 2},
             {"yard": 3, "material": "blue_block", "qty": 10}]}
```

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
| `sort/event/batch` | 0 (fault/done 时 retain) | `{phase: accepted\|done\|fault, job_id, source, actions, fault}` |
| `sort/event/dispatch` | 0 | 每条 divert 成功一次: `{job_id, sorter, material, qty, ts}` |
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
| `POST /dispatch` | `{actions, source?, path?}` → 202 `{job_id, queued}` / 409 故障锁存 / 422 非法 |
| `POST /fault/clear` | 人工清除故障 |

## 配置

- `config/sort_gateway.json`: broker / 端口 / 分拣器数 / ack 超时 / 重试 / 统计周期 /
  批间停带 (`stop_conveyor_after_batch`) / 队列深度 / 台账路径。
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
   .venv/bin/python tests/test_sort_mqtt.py  # E2E 31 项 (独立进程, 不需上面三个)
   ```
4. PLC 接入后, 用 `sim_devices.py --fault-rate/--no-ack` 的行为当故障测试参照。
5. 认证/TLS: 当前网关 paho 客户端**未配置**用户名/密码/TLS — 若生产 broker 需要,
   见 OPEN_QUESTIONS 第 6 条。
