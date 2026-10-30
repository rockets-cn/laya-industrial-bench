# Laya 工业决策回路（laya-industrial-bench）

在 [Laya](https://github.com/NandhaKishorM/laya)（非自回归 System 1 决策模型）之上构建并压测一条
**语音/文本工业决策回路**。本仓库 = 上游 Laya 引擎 + 本项目新增的回路、基准、教师与部署（下表已标注）。

## 架构

```
语音/文本 -> ASR(SenseVoice int8) -> 规则归一化 -> Laya 结构化决策(:8000)
          -> 确定性校验器 -> (LLM 旁路: 仅校验拒绝时) -> PLC 下发
```

物理执行侧 (分拣线): 语音/文本回路的 yard 域 actions → `sort_mqtt.py` MQTT 网关
(白名单校验 + 顺序执行 + 统计分发) → **3 分拣器 + 1 传送带** (MQTT,
协议契约 [`docs/sort-mqtt.md`](docs/sort-mqtt.md)); 视觉传感器
(`vision_input.py`, HSV 像素层识别; 或实体 HuskyLens 2) 发物料事件到同一网关,
网关按 `config/material_map.json` 映射并下发。无硬件时 `sim_devices.py`
模拟器闭环; 实体硬件用 **EDGE 101 固件** (`edge101/`, 与模拟器行为等价,
对网关零改动; RMT 硬件脉冲 + 推杆前侧光门时序触发 (未接线走虚拟光门预测)
见 `edge101/README.md`)。
E2E 见 `tests/test_sort_mqtt.py`, 固件单测
`edge101/tests/host/run_tests.sh`。

三条设计原则（完整论证见 [`VOICE_DECISION_BENCH.md`](VOICE_DECISION_BENCH.md)）：

1. **实时路径不含 LLM**。规则层做确定性抽取（颜色/数量/否定，ms 级），Laya 做单次前向的结构化
   决策（~120 ms/提及，白名单选项），校验器做白名单 + 数量范围 + 交叉检查。
2. **LLM（qwen3.5:4b，Ollama :11434）仅旁路/分配**。只有校验器拒绝时，或料场域结构模糊的
   分配指令（`EXPAND`，需总量对账）才调用；输出必须过同一个
   校验器；LLM 不可用或输出不可信时，安全降级为 **暂停 + 人工确认**（PAUSE+HUMAN），绝不放行。
3. **LLM 的离线贡献**。新增领域时由离线教师（`teacher_*.py`）生成语料、交叉标注、审计规则层、
   产出 Laya 微调数据——全程离线，不进实时路径。

## 支持的领域

| 领域 | 输入例 | 输出 | 回路 |
|---|---|---|---|
| 水果 | "来两个红苹果和一把香蕉" | `[{action: red_apple, qty: 2}, {action: banana, qty: 1}]` | `bench_industrial.py` |
| 彩色方块 | "10个红色方块，5个绿色方块，7个黄色方块" | `[{color: red, qty: 10}, {color: green, qty: 5}, {color: yellow, qty: 7}]` | `bench_block.py` |
| 混合 | "两个红苹果和10个红色方块" | 两条规则层并行、各抽各的，动作合并 | `loop_ui.py` `mixed` 模式 |
| 料场分拣 | "给1号料场送两个红苹果，3号料场送10个蓝色方块" | `[{yard: 1, material: red_apple, qty: 2}, {yard: 3, material: blue_block, qty: 10}]` | `bench_yard.py` |
| 料场·分配 | "九十九个香蕉平均分到三个仓库里去" | `[{yard:1,qty:33},{yard:2,qty:33},{yard:3,qty:33}]`（见下「分配指令」） | `bench_yard.py` |

领域可插拔：每条回路都是 `normalize()`（词表/正则）+ `laya_decide()` + `validate()` 三件套，
换词表即新领域。视觉侧另有 `bench_colorblock.py`：Laya 看不到像素，颜色块识别由像素层
HSV 阈值完成（mean 3.6 ms / p95 4.6 ms），Laya 只在下游做决策与交叉校验。

### 分配指令（料场域）

「把 N 个 X 平均/平分到 M 个仓库」这类指令分两层处理，**任何 LLM 输出都过校验器**：

1. **Tier-1 确定性展开**（毫秒级，无 LLM，路径 `FAST`）：规则层直接做除法，
   整除均分；除不尽时**余数给小号料场**（如 `100 个香蕉 → 1号 34 / 2号 33 / 3号 33`）。
   目标解析：`M 个仓库`（M=1..3 → 前 M 号）或点名 `1号和3号`。
2. **Tier-2 LLM 展开**（路径 `EXPAND`）：结构模糊（未指明哪几个料场等）时，LLM 只判断
   **料场集合**；数量由规则层按「余数给小号料场」**确定性重算**（实测 4b 模型不遵守
   prompt 里的余数规则——LLM 的 qty 只当参考值）。校验器做 **总量对账（容差 0）**：
   展开总量必须精确等于指令里的 N，物料必须与规则层听到的物料一致；对不上即
   **PAUSE+HUMAN**，提案展示给人工。

即：**能算的不让 LLM 算, 算不了/说得模糊的才让 LLM 算, 且 LLM 永远过校验器。**
无数量的分配句（"把香蕉平均分到仓库里"）不触发 LLM, 照旧 PAUSE+HUMAN 等人工。

### 超纲料场与部分下发（料场域）

只有 1/2/3 号在纲, 超纲的 4号/5号/七号… 由 规则层参与分域归属 + 校验器路由:

- **混合订单混入超纲项**（路径 `PARTIAL`, 橙色徽标）: 在纲项照发（按原话数量, 不重算）,
  超纲项挂起并附「超纲料场 N 号不下发 (物料 xN, 需人工确认)」。
  例: 「一号料场放三个苹果, 2号料场放4个苹果, 5号料场放6个苹果」→ 1号×3 + 2号×4 下发,
  5号×6 挂起。页面「📤 下发分拣线」按钮对 PARTIAL 同样可用（只含在纲指令）;
- **方块未说颜色**（同走 PARTIAL 挂起通道）: 「一号放三个方块, 2号放4个苹果」→
  2号×4 照发, 1号方块挂起「1号 物料未定, 不下发 (x3, 需人工确认颜色/种类)」—
  白名单 4 色无法定夺, 规则层与 LLM 都不猜颜色; 纯未定单仍整条 PAUSE+HUMAN;
- **纯超纲单 / 分配指令目标含超纲**: 整条暂停 PAUSE+HUMAN — 分配句的数量是除出来的,
  目标集合少一个在纲项就会分多, 规则层不重算, 也不让 LLM 重算（Tier-1/Tier-2 均拒展开）;
- 归属保证: 超纲号占自己的域, 「5号放6个苹果」不会并入邻近 2号 域。

### 简单问答（料场页面, `local_qa.py`）

问句而非分拣指令时（路径 `QA`），纯规则层（零 LLM, 毫秒级）直答：

- **类别**: 时间/日期（"现在几点了"）、服务状态（"8002还在跑吗" → 本机 TCP 端口探测
  8002/8000/8123/11434）、资源占用（"CPU占用多少" → /proc/loadavg + meminfo + disk）、
  料场库存（"1号料场有多少件" → 最近一次分拣指令, loop_ui 每次 /run 后注入）、
  累计库存（"现在各个仓库分别有多少内容" / "仓库库存多少" → SQLite 出入库台账
  `yard_ledger`, 分拣下发自动记入, 入-出; 门 `_STOCK_RE` 各/分别/库存/总共, 空库答「暂无库存」）;
- **硬约束**: 答案 ≤ 30 字（超出截断）; 全程 3 s 超时预算（端口探测 0.4 s/个）,
  查不到/超时不编造——该句回落普通分拣路径（空订单 → PAUSE+HUMAN）;
- **防误抢**: 句中含物料词（香蕉/苹果/梨/方块）视为分拣指令, 不进问答。

**LLM 兜底层（默认关）**: 规则层认不出的长尾问句（组合/措辞差异）可交给 LLM 基于
**注入事实**（当前时间/服务端口实测/资源/料场白名单/最近分拣指令）作答：
- **页面开关**: 输入行「☑ LLM 兜底」复选框（运行时切换, 无需重启服务, 状态存浏览器
  localStorage, 每次 /run 请求携带 `qa_llm`）; 或启动默认: `QA_LLM=1 python3 loop_ui.py`
  （模型默认 qwen3.5:4b, `QA_LLM_MODEL` 可覆盖）; 请求未带 `qa_llm` 时以环境变量为准;
- 纪律: 只转述注入事实, 事实外答「不知道」; 答案只展示、QA 路径无 PLC 输出;
  超时(20s)/失败/空答案 → 回落分拣路径（PAUSE+HUMAN）; 页面徽标 `💬 简单回答 · LLM 兜底`。
  默认关的原因: 本机 4b 实测 10~16s/请求, 规则层能确定性回答的问题绝不绕道 LLM。

## 快速开始

```bash
# 1) 环境 + checkpoint（models/, gitignored, ~2.3 GB）
./setup_laya.sh

# 2) Laya 推理 API（Jev 兼容, :8000）与 LLM 旁路（:11434）
.venv/bin/python jev_api.py    # 或 systemctl --user start laya-jev-api
ollama serve                   # 或 systemctl --user start ollama-serve

# 3) Web 控制台（:8002）— 领域切换 水果/方块/混合/料场分拣、预设、
#    麦克风授权 + 按住说话/录音识别 (唤醒词「优优」可选开关)、全链路 trace
.venv/bin/python loop_ui.py    # 或 systemctl --user start laya-loop-ui

# 4) 基准
.venv/bin/python bench_industrial.py 3       # 水果域, 语音全流程 (test_audio/tts_*.wav)
.venv/bin/python bench_industrial.py 3 text  # 水果域, 文本模式
.venv/bin/python bench_block.py 3            # 彩色方块域
.venv/bin/python bench_yard.py 3 text        # 料场分拣域 (1/2/3 号料场 × 8 物料)
.venv/bin/python bench_oob.py                # 超纲验收 (PAUSE+HUMAN)

# 5) 分拣线闭环 (MQTT: 1 传送带 + 3 分拣器, 模拟设备)
.venv/bin/python broker_local.py             # 终端 1: MQTT broker (:1883)
.venv/bin/python sim_devices.py              # 终端 2: 模拟传送带 + 3 分拣器
.venv/bin/python sort_mqtt.py                # 终端 3: 分拣网关 (HTTP :8123, 统计分发)
.venv/bin/python vision_input.py --gen      # 可选: 视觉传感器 (合成帧, 摄像头 --camera N)
# Web 控制台 (loop_ui) 「📤 下发分拣线」按钮: Laya 分类结果 → 网关 → MQTT → 设备
# E2E 测试 (自包含, 不依赖上面进程): .venv/bin/python tests/test_sort_mqtt.py
```

**开机自启**（本机已装好，`deploy/` 可在别的机器复现）：三个 systemd 用户服务
`ollama-serve` → `laya-jev-api` → `laya-loop-ui`（均 `Restart=on-failure`），
外加 XDG autostart 条目在桌面会话启动时等服务就绪（≤60 s）后自动开 Chromium 窗口。
安装与日志方法见 `deploy/*.service` 头部注释：

```bash
systemctl --user status ollama-serve laya-jev-api laya-loop-ui
journalctl --user -u laya-loop-ui -f
```

## 实测性能（8 核 CPU + iGPU；SenseVoice int8 常驻 + Laya CPU）

| 指标 | 数值 |
|---|---|
| ASR（SenseVoice small int8） | ~100–250 ms |
| Laya 决策（每提及一次 HTTP 请求） | mean ~115–134 ms |
| 水果域语音全流程 E2E | mean 334 ms / p95 488 ms，快路径 12/12 |
| 方块域 E2E（快路径） | ~230–360 ms |
| 混合域 E2E（warm） | ~200 ms |
| 水果域留出集准确率 | base 89.2% → 微调 `laya-fruits-teacher` **100%**（裸词"苹果"漂移 35/50 → 50/50） |
| 方块域规则层交叉校验 | 95.9%（qwen4b 教师可信度 96.8%；修复盲区前 75.3%） |
| 超纲句 | 一律 PAUSE+HUMAN（安全降级，不产出动作） |
| 交互方式 | PTT（按住说话）对比 唤醒词+自动断句：每句省 ~1.2 s 句尾静音等待 + 唤醒词往返，实测响应快很多（论证见 `VOICE_DECISION_BENCH.md` §4「交互方式经验」） |

完整数据与结论：[`VOICE_DECISION_BENCH.md`](VOICE_DECISION_BENCH.md)、[`TEACHER_REPORT.md`](TEACHER_REPORT.md)、
[`BENCHMARKS.md`](BENCHMARKS.md)、[`DEPLOY_SENSEVOICE.md`](DEPLOY_SENSEVOICE.md)。

## 仓库结构

**本项目新增**：

| 路径 | 说明 |
|---|---|
| `bench_industrial.py` | 水果域工业回路基准（语音/文本；含 LLM 旁路与 PAUSE+HUMAN 安全降级） |
| `bench_block.py` | 彩色方块域回路（颜色×数量；简繁/同音/省略/后置数量/混水果） |
| `bench_yard.py` | 料场分拣域回路（1/2/3 号料场 × 8 物料；料场号规则层决定+校验、物料 Laya 终审、倒序/同音/否定、超纲暂停） |
| `bench_colorblock.py` | 颜色块像素层识别基准（HSV 阈值 + 可选 Laya 交叉校验；`--camera/--file`） |
| `bench_fruit.py` | 纯 Laya 水果描述分类（延迟分位 + 分类准确率） |
| `bench_pipeline.py` | LLM 意图整理 → Laya 终审 对比管线（`llm_json`/`http_post` 工具函数） |
| `bench_jev.py` / `bench_split.py` / `bench_oob.py` / `bench_voice.py` | API 基准 / 单请求多问 vs 拆分 A/B / 超纲验收 / 全语音管线 |
| `jev_api.py` | Jev 兼容 HTTP API（`:8000`，`POST /v1/systemone`；模型含 `jev-fruits` 微调模型） |
| `loop_ui.py` | Web 控制台（`:8002`）：领域切换（水果/方块/混合/仓库分拣）、预设、麦克风授权+按住说话/录音识别（唤醒词可选开关）、全链路 trace；两段式语音（`/asr` 先出识别文字, `/run` 复用不重复识别, 失败回落单发）；纯转发不载模型 |
| `teacher_offline.py` | 水果域离线教师：qwen4b 造语料 + 交叉标注 + Laya 微调 |
| `teacher_blocks.py` | 方块域离线教师：生成 + 交叉标注 + 规则层审计 → `teacher/blocks_corpus.jsonl` |
| `teacher/` | 语料与训练/评测数据（jsonl） |
| `models/` | checkpoint 与微调模型（`laya-fruits-teacher` 等，gitignored） |
| `test_audio/` | 基准用 TTS 音频与录音 |
| `deploy/` | 开机自启：systemd 用户服务 ×3 + 浏览器 autostart |
| `scripts/open_loop_ui.sh` | 开机时等服务就绪后开浏览器 |
| `VOICE_DECISION_BENCH.md` / `TEACHER_REPORT.md` / `DEPLOY_SENSEVOICE.md` | 链路设计报告 / 教师报告 / 部署记录 |
| `sort_mqtt.py` | MQTT 分拣网关: 入站 (loop_ui /dispatch · SCADA `sort/inbound/command` · 视觉 `sort/inbound/vision`) → 白名单校验 → 顺序执行 → 统计分发 (JSONL 台账 + `sort/stats/summary`); 命令超时重试 → 故障锁存 → 人工清除; HTTP :8123 (`/health` `/stats` `/motion` `/dispatch` `/fault/clear`) |
| `yard_ledger.py` | 料场出入库台账 (SQLite, `data/yard_ledger.db`): 分拣下发自动记入, 页面手动记出, 库存=入-出; `record_in/record_out/stock/recent` |
| `sim_devices.py` | 设备模拟器: 传送带 + 3 分拣器状态机 (按件计时, 真实时序), 2Hz 遥测, 故障注入 (`--fault-rate` `--no-ack`), 光门模式 (`--photo-wait/--photo-timeout/--photo-dead-sorter`), 运动事件 (`sort/line1/motion`, 与固件契约一致); 真实 PLC 接入时的行为参照 |
| `broker_local.py` | 本地 MQTT broker (amqtt, 纯 Python, :1883); 生产建议 mosquitto (topic 约定不变) |
| `vision_input.py` | 视觉传感器接入: 帧 (合成/图片/`--camera N`) → HSV 像素层识别 4 色方块 (ms 级) → 发 `sort/inbound/vision`; 外部带模型传感器可直发同 topic 跳过本模块 |
| `edge101/` | **物理控制器固件** (EDGE 101 / DFR1283, ESP32): 板载 RJ45 + HuskyLens 2 (UART) + 步进 (MKS SERVO42C 闭环, RMT 硬件脉冲, EN 走 MCP23017) + PCA9685 I2C 3 舵机 + 3 路光门 (推杆推出时序触发, MCP23017 PB0/1/2; 未接线时虚拟光门按带面位置预测); 与 sim_devices 行为等价, 直接实现 `docs/sort-mqtt.md`; 主机单测 `tests/host/run_tests.sh` (141 项), 接线 `docs/wiring.md`, 构建/烧录见 `edge101/README.md` |
| `config/sort_gateway.json` / `config/material_map.json` | 网关配置 / 物料→分拣器映射 (**占位**, 待现场确认, 见 `OPEN_QUESTIONS.md`) |
| `tests/test_sort_mqtt.py` | 分拣网关 E2E 52 项 (进程内 broker + 模拟设备 + 网关): 下发/故障恢复/视觉/SCADA/白名单/统计/divert 时序回归/光门触发 (trigger=photo + 坐标透传)/运动事件时间线 (sort/line1/motion + GET /motion + pos_mm 透传) |
| `docs/sort-mqtt.md` | MQTT 分拣协议契约 (命令/应答/遥测 topic, 状态机, 白名单, 安全降级, PLC 接入清单) |
| `edge101/docs/gate_layout_anim.svg` | 分拣线光门运行示意（俯视 · 动画版 · 数字孪生双模式）：3 路时序光门装在各推杆前侧/上游；内嵌 rAF 循环双模式 — 离线=循环演示（带速 1.0 m/s、时间 2:1 放慢、推杆 400ms、45° 料道拨入），在线=按网关 `sort/line1/motion` 时间线实时还原；`loop_ui` 光门折叠区优先加载此版, 缺失回落静态版 `gate_layout.svg` |
| `OPEN_QUESTIONS.md` | 未决问题清单 (物料映射/水果视觉/防重复/PLC 细节…), 待讨论 |

**上游 Laya 引擎**（原样保留，文档见 [`LOCAL_SETUP.md`](LOCAL_SETUP.md)）：
`laya/`（Python 包）、`sdk/`、`docs/`、`research/`、`benchmarks/`、`notebooks/`、
`laya-dotnet/` `laya-java/` `laya-ts/`、`tests/`、`verify/`、`docker/`。

## 仓库台账（SQLite 出入库统计）

统计「放入了各仓库多少内容」：

- **入库（自动）**：分拣回路每次产生可执行下发（FAST / EXPAND / PARTIAL）时，
  按每条 action 记一行入 `data/yard_ledger.db`（`moves` 表）；PAUSE / QA / 空订单无动作不记账；
  超纲挂起项（PARTIAL 的 oob）不下发、不记账。
- **出库（手动）**：页面「📦 仓库台账」面板内的出库表单
  （`POST /ledger/out`，料场 1-3 + 8 物料白名单 + 数量 1~999，越界 422）。
- **库存**：按 料场×物料 维度 累计入 − 累计出；`GET /ledger` 返回库存 + 最近 20 笔流水。
- **页面**：「📦 仓库台账」按钮展开面板——三料场库存卡（净件数 + 物料明细）+ 最近流水
  （入=绿/出=橙 + 路径标记）+ 出库表单；面板开着时每次成功下发自动刷新。
  负库存如实显示（出大于入，提示人工核对）。
- **常驻库存**：仓库信息（三料场卡）上方常驻一行卡片
  （`GET /stock`，每仓一个卡: 净件数 + 物料明细, 与「仓库信息」同风格, 外加总库存卡），
  页面加载即显示、每次运行后刷新, 不用点台账面板就能看到库存；
  台账面板仍是全量明细（流水 + 出库表单 + 清空）。
- 台账是统计旁路：记账失败只打日志，绝不影响分拣下发主路径。
- **问答联动**：「现在各个仓库分别有多少内容」等累计库存问句由规则层（`local_qa`）
  读本台账毫秒级直答（QA 路径）；LLM 兜底开启时，当前库存也作为注入事实的一部分。
- **清空**：页面面板内「🗑 清空台账」按钮（二次确认, `POST /ledger/reset`）,
  或命令行（服务运行中均可, 无需停止）:
  `python3 -c "import sqlite3; c=sqlite3.connect('data/yard_ledger.db'); c.execute('DELETE FROM moves'); c.commit(); c.execute('VACUUM')"`
  两者都等价于重建空库；删文件方式（`rm data/yard_ledger.db`）必须先停服务再重启, 不推荐。

## 分拣线光门运行示意（动画版 · 数字孪生双模式）

`edge101/docs/gate_layout_anim.svg`（静态参考版 `gate_layout.svg` 仍在）：

- **布局**：俯视 3 路分拣器，每路时序光门装在该推杆**前侧（上游）**；相机在带首，
  推料点→45° 料道拨入各仓库。
- **双模式**（同一 SVG 内嵌脚本，按 `window.__gla` 钩子切换）：
  - **循环演示**（默认/离线）：rAF 循环按固件口径取真实值再整体 2:1 放慢：
    带速 1.0 m/s（1px≈3.3mm）、光门落沿（物品前沿触光束）→ 推杆 400ms 伸出 →
    物品中心到达推料点（D = 带速 × 500ms 对到）→ 45° 滑入料道（料道高亮）；
    相机扇形在物品过相机 0.5s 内闪烁；底部状态行随最近事件滚动；循环播放（周期 ≈14.2s）。
  - **数字孪生同步**（网关在线）：页面轮询 `GET /twin`（`loop_ui` 代理解析网关
    `:8123` 的 `/health`+`/motion`+`/stats`）→ `__gla.setTwin()` 切实时模式：
    每分拣器按 `sort/line1/motion` 时间线最近事件还原状态
    （armed=推杆推出中 450ms / busy=蓄能+推出拨入 ≈900ms / fault=光门红告警 /
    ready=静止），传送带按 `belt_mps` 平移、有 job 在跑才动（网关批间停带, 忠实还原）；
    状态行显示最后事件 + 时间。离线（网关不可达）自动回落循环演示，
    页头状态条 `#gla-mode` 显示「实时同步/循环演示 + 带速 + 运行中/待机」。
- **采样节奏**：在线时自适应轮询 — 有 job（active_job/queue>0）400ms, 待机 600ms,
  离线 1500ms。任务窗口 ~1.3s（photo 时序: 光门等待 350ms + 拨件 900ms）,
  固定 1.5s 轮询会整单漏掉 busy 态。`ts:""` 的事件（真机未实现时间戳时）不驱动时间线动画。
- **尊重偏好**：`prefers-reduced-motion: reduce` 时停止动画，静态布局与文案有效。
- **双合法**：同一文件既要是合法 XML（独立打开），也要能 HTML 内联进 `loop_ui`
  折叠区——因此内嵌脚本不用 CDATA、不写裸 `<`（比较一律反向 `x>=t`）、
  不写 `&`（逻辑与用嵌套 if）、注释内不出现 `--`。
- **验证链**：`xml.etree.ElementTree` 解析 → 提取脚本 `node --check` →
  行为桩（`test_gate.js`：逐事件断言 传送带/相机/光门挡光/推杆伸出/45° 轨迹/循环回绕）
  → 真浏览器 CDP 采样 DOM 确认动画在动（离线循环 + 在线孪生两态）。
- **注意**：若局域网真机（edge101）与 sim 同时连在同一 broker, 同一 job 会出两条
  运动时间线（真机虚拟光门无视觉锚点时回退 `via:immediate`, 紧凑 JSON）——动画取
  每分拣器最近事件, 状态不受影响, 但 busy 窗口可能双重表现。
- 固件时序若调整（另一工作线），按本节口径同步更新本 SVG 的常量（`V/G/P/DT/TPU/CHV/CHL`）。

## 新增一个领域（LLM 离线贡献流程）

1. 照 `bench_block.py` 抄三件套：`normalize()`（新词表/正则）+ `laya_decide()`（白名单选项）
   + `validate()`（范围/交叉检查）；
2. 照 `teacher_blocks.py` 跑离线教师：qwen4b 生成口语新句（含 ASR 噪声、混入、否定）→ 逐句交叉
   标注 → 与规则层对拍，mismatch 报告直接定位正则盲区；
3. 语料存 `teacher/*.jsonl`；精确度不够时按 `teacher_offline.py` 的路子微调 Laya
   （base 89.2% → 100% 的做法）。

## 上游 Laya

决策引擎本身（模型、安装、SDK/API 参考、51 语言基准、微调教程）属于上游项目，本 README 不复述，
直接看仓库 <https://github.com/NandhaKishorM/laya> 与其文档站
<https://nandhakishorm.github.io/laya/>。本项目对引擎的全部用法是：
`Router().predict(state, questions)` 单次前向回答全部问题，由 `jev_api.py` 包成
Jev 兼容 HTTP API 供回路调用。

## License

Apache 2.0（沿用上游 Laya 许可；上游由 Convai Innovations 开发）。
