# 分拣线接入 · 未决问题 (待讨论)

> 2026-11-19 更新。原 2026-10-29 夜自动推进时**没有结论、需要拍板**的部分保留在下方;
> 已实现内容见 `docs/sort-mqtt.md`、`edge101/`。

## 2026-11-19 已完成 (方案 B: 物料校验式下发)

> 用户报障 "1号仓库: 超声波 ×1, 实际推了两个旋钮" → 定案: **语音指令=需求不盲拨,
> HuskyLens2 逐件核对物料后才推**。用户批准实施 ("好的，干吧")。

| 项 | 说明 |
|---|---|
| `dispatch_mode: "blind" \| "verified"` (默认 `blind` = 现状零风险) | `verified`: 语音 job 不发盲拨 divert(qty=N) — 只做起带+登记需求 (`remaining=N`, `deadline=verified_timeout_s` 默认 90s) 转「待核对」; HL2 来件匹配需求 → **逐件** `divert qty=1 trigger=photo` (逐件 job 带 `parent_job_id` 归属父语音 job); 不匹配 → **直通带尾** (verified 语义下移除 material_map 兜底, 不自动推); `remaining=0` → job done; 超时 → **故障锁存+停带** + `sort/event/verified{phase:timeout}` |
| 进度记账 | `_verified_progress(job_id, done\|fault)`: 逐件 done → pushed+1 / remaining-1, 耗尽 → 父 job done; 逐件 fault → 父 job fault (机器故障由逐件 job 锁存, 父 job 只记账不重复锁存) |
| 新接口 | `POST /dispatch/cancel {job_id}` (200 撤销+需求作废 / 409 执行中 / 404 不存在); `GET /health` + `verified[]` (job_id/material/pushed/remaining/deadline_in_s); `/jobs` + `pushed`/`remaining` + `cancelled` judgment |
| 页面 (loop_ui) | 下发应答 "物料校验模式: 已登记需求, 等逐件核对" 提示; 孪生状态行 "🔍 待核对 pushed X/Y (超时余 Ns)"; 对照表 ✎已撤销/🔍核对 记账 |
| 验证 | sim E2E **95/95** (77 回归 + 18 verified 新增: 不盲拨 / 不匹配直通不耗需求 / 匹配逐件×2+进度 / 无需求无兜底 / 超时故障+锁存期 409 / 人工清障 / 撤销+需求作废+404); bench_yard 40/40 不变; JS `node --check` 过 |
| 生产切换 | `config/sort_gateway.json` 暂保持 `blind`; **实机回归通过后**改 `"dispatch_mode": "verified"` + `systemctl --user restart laya-sort-gateway` |
| 解决 G 遗留 | **双下发结构性消除**: verified 下语音 job 不盲拨, 所有下发都经 HL 逐件核对路径 (旧 `vision_dispatch=true` 的叠加风险消失); "无指令不推" 语义在 verified 下也成立 (无需求 → 直通) |
| 已知边界 | ① 密集同物料 (两件 <0.8s) 欠发是**存在性去重窗口**的独立遗留 (OPEN G 节幻影件修复的副作用), 本方案不解决, 彻底方案=位置式下发 (固件 vision 包填 pos_mm); ② verified 超时期间新指令 409 需先清故障 (与其他故障一致) |

## 2026-11-13 已完成 (4 传感器迁移 + 语音指令路由 v2)

> 用户定案: ① 选仓用**语音指令指定**; ② 物料集加 4 传感器; ③ **真实线只用传感器, 不用水果/方块**。

| 项 | 说明 |
|---|---|
| 物料集 8 水果/方块 → **4 传感器** | `angle_sensor 角度 / ultrasonic_sensor 超声波 / pir_sensor 人体红外 / temp_humidity_sensor 温湿度`。白名单同步点: `yard_ledger.py` / `bench_yard.py` / `local_qa.py` / `loop_ui.py` / 动画 SVG 图标 + `POOL12` 供血池。旧 8 物料仅 sim 演示域兼容保留 |
| **路由 v2: 语音指令指定仓库** | 选仓权从固定映射表移到**网关指令需求台账** (`sort_mqtt.py` `demands[material]` FIFO, 条目 `{yard, remaining, job_id, t}`, TTL 180s): `/dispatch` 受理即登记 (job 开始, 早于物理执行), 视觉事件按台账消耗路由; 失败/撤销 job 作废需求; `source=vision` 不回填 (防台账自我膨胀)。`config/material_map.json` 降为**无指令兜底** + 遗留 `vision_dispatch=True` 自动分拣 |
| HuskyLens2 定制模型 | algo=129 (YOLO 定制), 4 传感器按**名字**识别 (ID 全 0); 旧固件无真实 conf → 网关 `math.copysign` 判符号放行 (见下方踩坑节) |
| 验证 | bench_yard 185/185; 网关 E2E **77/77** (含 §3b 指令路由优先 5 断言: temp_humidity_sensor 映射表 1 号仓 vs 指令 2 号仓, 视觉先到 2, 耗尽回退 1); 固件主机单测 172/172; 动画 DOM 25/25 |
| ASR 同音/变体加固 (SenseVoice 探针实测) | ① 词表加 **温度湿度** 变体 ("温湿度传感器" 被识为 "温度湿度传感器"); ② **对称后置数量** "角度传感器五个红外传感器三个" (ASR 语序, 数量在物料后) 按对称确认归位; ③ 无料场标记但句中有物料: 不再静默丢弃 → 报 **"料场未指定, 不下发 (×N, 需人工确认)"** 挂起 (原笼统"空订单"); ④ 该情形跳过 LLM 旁路 (幻觉防护只放行听过的 (料场,物料) 组合, 旁路救不了, 省几十秒等待)。实测: 仓→舱 / 进→接/g / 逗号丢失 等 ASR 噪声全部容忍, 10 句 TTS→ASR→/run 全链路正确 |

> 注: 密集料空推 (A) / 薄件 IR (C) / 3 号仓 ±3mm (D) 与本轮无关, 仍挂起。

## 2026-11-11 已知待办 (后续解决, 用户定"先挂着、后面想办法")

> 本轮已完成并入库 (HEAD `16afccf`): 队列 + 皮带位置 P 到仓固件、网关带速定案 0.0525m/s、
> IR T0 UI 同步、推杆↔真机舵机孪生联动 (1/2/3 仓均验过)、网关单测 70/70。
> 以下为实机暴露、**尚未解决**的问题 (按优先级)。

### A. 密集料空推 / 叠件串拨 (核心, 最高优先)
- **现象**: 件间距 <52mm 时 (实测 lm2/lm3 仅 10mm、lm4/lm5 仅 8mm; PCB 宽 30~40mm),
  推杆一次扫两件 → 第一件推杆把 2 件一起拨进仓 → 轮到第二件时仓口已空 → **空推**;
  固件"已推"计数偏多 (报 6 推, 物理仅 4 次有效); 密集混仓时叠件串拨会**串料**。
- **根因**: 料比仓密 (仓距 1→2=62mm、2→3=52mm, 料距 <50mm), 单点 IR + 单推杆无法区分叠件。
- **待选方案 (未定)**:
  ① 固件检测空推 (推完查仓口占用, 需仓口加占用传感器);
  ② 上游加隔离闸 / 产线侧约束料距 >52mm;
  ③ 仓口占用传感器回报, 空推不计入"已推"计数。
- **现状**: 用户选 A (只要每件进对仓即可, 不强制严格放件推序); 密集料场景仍会空推, 需上方案之一。

### B. 孪生带面无物料显示 (已定位, 非 bug; 待 HuskyLens 模型)
- **现象**: 网页(真实源)推杆伸/收与真机舵机联动正常, 但**带面无物料**。
- **定位**: 网页带面物料 = HuskyLens 视觉事件 (固件 `[hl]` → `sort/inbound/vision` →
  网关 `_on_vision` 按 `config/material_map.json` 映射 yard → `/twin` vision → 网页 3s 窗口上带)。
  手动 divert (MQTT) **不产生 vision 事件** → 带面空 (预期现象, 非网页 bug)。
- **已验证 (2026-11-11)**: 用固件 `vision <material>` 串口注入模拟, 3 件
  `blue_block→仓1 / red_block→仓2 / banana→仓3` 全部被网关正确映射并透传 `/twin`,
  「视觉事件 → 网页带面上料」链路通 (脚本 `/tmp/sim_vision.py`)。
- **已解决 (2026-11-13)**: 生产线物料集改为 **4 传感器**, HuskyLens2 切**定制模型**
  (algo=129 YOLO, 按名字识别, 4 种全中), `edge101/include/config.h` 的
  `MATERIAL_MAP` 占位已随定制模型弃用 (识别名直接是物料 key, 网关白名单/台账按名路由);
  conf 处理见下方「HuskyLens2 定制模型」踩坑节。
- **遗留 (2026-11-11 记录)**: 曾用固件 `vision <material>` 串口注入模拟验证链路,
  当时物料还是 `blue_block/red_block/banana` (占位映射, 已随 4 传感器迁移作废)。

### C. 薄/透光物体 IR 不触发
- **现象**: 薄/透光 PCB 在带面上, 单点红外接近 (GPIO34) 偶发不触发 T0。
- **待做**: 调 IR 灵敏度 / 加兜底 (视觉事件 + 带速外推补 T0) 或换传感器。未量化。

### D. 3 号仓精度 ±3mm
- **现象**: 3 号仓偶发上游 3mm (偏早)。根因未定 (物体放置抖动 vs IR 触点 vs 推杆行程)。
- **待做**: 标定复核或加末端对账。

### E. 全链路上机验证未完整
- **已跑 (2026-11-13)**: 语音 → ASR → 规则层 (/run) 链路, edge-tts 10 句 → SenseVoice → /run 全对 (见上方完成节 ASR 行); 仍**未跑**: 真实麦克风音频、网关 `POST /dispatch` 真流程 (本轮均为手动 MQTT/串口触发)。
- **待做**: 上实体后跑一次全链路回归。

### F. vision 去重"3s 窗口"语义错 — 同一件在视野内被重复下发 ✅ 已修 (8c07198, 2026-11-16)
- **修复**: `_on_vision` 改**存在性去重** `vision_dispatch_absence_s=0.8` (即本节"待选方案"的
  件生命周期语义: 同一物料间隔 ≤0.8s = 同一件持续在视野内, 不再下发; 出视野后新件入才再下发)。
  一件 = 一条 divert, 结构上不可能超发。配置键 `vision_dispatch_dedupe_s` 废弃 (单测改用 0.0 语义)。
- **验证 (2026-11-16 实机)**: 修复前当日 4 起"队列满"故障 (含带子 running 期间 16:32:17,
  密集料 1~3s/件下发 vs IR 消费积压); 修复后 2 件料 = 恰好 2 条下发 (1:1), 无幻影件、无故障。
  网关单测 77/77。
- **遗留 (挂起)**: 密集**同物料** (同料两件间隔 <0.8s 且同视野) 会欠发 → 个别件直通不分拣。
  彻底方案 = 位置式下发 (固件 vision 包填 `pos_mm`, 现为 null), 与密集料串拨 (OPEN #1) 配套做。
- **现象**: `vision_dispatch_dedupe_s=3.0` 是"同物料每 3s 允许再下发一次", **不是**"每件一次"。
  件在相机视野内停留 >3s (带子反复为推别的件而停、该件滞留在视野没走) → 每 3s 重新下发一条 divert
  → 固件队列灌入幻影件 → **空推 / 把恰好过 IR 的别的件错推进仓** (固件 IR 沿只认"推几号仓", 不核对物料)。
- **实测证据 (2026-11-13)**: 1 件人体红外滞留视野 ~36s, 共产 **10 次 pir 下发** (1 次语音指令
  `loop_ui` + 9 次 `vision` 重下发, 每 3~9s 一次, job j29~j39)。物理仅 1 件, 队列却灌 10 条"推 3 号仓"。
  路由本身正确 (语音"人体红外去3号仓"→ 需求台账 pir→3; material_map 仅兜底), 病根在去重。
- **根因**: 去重是**时间窗口**, 不是**件生命周期**。
- **待选方案**: 改"件离开视野后重新武装"(上升沿检测): 同件不管停留多久只下发一次, 离开视野、
  下一件进来才再下发。需定 `vision_dispatch_gap_s`(判"件已离开"阈值, 建议 ~1.0s, 大于 HL 上报间隔 0.3s、
  小于件距行程时间)。
- **现状**: 2026-11-16 已修 (见上), 原"挂起"解除。
- **关联 (本轮已落, 未提交)**: ① 网关 `_send_with_retry` 认固件 divert 即回 `queued` 为"已受理"不重发
  (此前只认 ok/busy/fault, `queued` 掉"无应答"分支 → 重发 divert → 固件 `g_count+=qty` 重复入队,
  是队列积压的另一真凶); ② 单测 cfg 注入 `vision_dispatch_dedupe_s=0.0` 走离散事件语义
  (一件=一条 vision), 生产配置仍 3.0s → 已由 8c07198 改为 `vision_dispatch_absence_s=0.8`。

### G. 无指令自动推 + 有指令双下发 + 需求被空耗 (2026-11-18, 用户报"没指令也自己推")
- **用户报障**: ① 网页无法手动起带/推杆 (故障锁存 409 且页面无清障入口); ② 放任何东西都自动推, 没指令也推。
- **排查证据**: 故障前 27 条下发全是 angle→1 号仓, IR 光门只过 5 件 → 22 条幻影件积压 → 队列满故障。
  故障锁存期 `/belt` 与 `/push` 均 409, 页面卡死无恢复入口 (当时只能 curl)。
- **三个真 bug**:
  1. **无指令也推**: 生产 `vision_dispatch: true` → `_on_vision` 无需求时回退 `material_map` 照样下发
     (HL 自动分拣模式)。用户语义 = 无指令物料应直通带尾。
  2. **有指令双下发**: 语音 job 盲拨 divert(qty=N) **与** vision 路径按需求逐件下发叠加 → 每件 2 条队列项
     → 净幻影累积 → 照样队列满 (存在性去重只去重 vision 路径自身, 去不掉双路径叠加)。
  3. **需求被空耗**: `_demand_route` 在**每个**视觉事件都 `remaining-=1` → 一件在视野停留 5.7s (上百帧)
     把整条需求耗光, 后续件丢失路由。
- **修复 (本轮)**:
  1. 生产配置 `vision_dispatch: false` → 无指令物料直通带尾不自动推; 指令仍走语音 job 盲拨下发
     (此时不存在双下发)。
  2. `_demand_route(material, consume=True/False)`: /vision 去向标注用 peek 不消耗, 需求仅在真正
     下发时消耗一次 (修空耗)。
  3. 页面「⚠️ 清故障」按钮: loop_ui `/api/fault/clear` → 网关 `/fault/clear` (清锁存 + 固件 reset 清队列);
     仅真实源且故障锁存时显示, 清后起带/推杆恢复 (修问题①恢复路径)。
- **单测**: 77/77 (测试保持 `vision_dispatch: true` 覆盖该特性, 不受生产配置影响)。
- **遗留 (2026-11-19 已闭环)**: 双下发 — 由**方案 B `dispatch_mode=verified` 结构性消除**:
  语音 job 不盲拨, 只登记需求; 所有下发都走 HL2 逐件核对路径 (匹配才推, 不匹配直通带尾)。
  实机回归后生产切 `verified` (见文首 2026-11-19 节)。

### H. 固件 HL2 轮询无去重 — 一件被"识别" ~19 次 → 仍有重复 → ✅ 两段修完 (2026-11-19)
- **用户报障**: "实际用 HuskyLens2 看到一个会产生好多个, 应该避免重复识别";
  第一段修复后仍报"还是会有出现重复多余的传感器"。
- **实测证据**: 网关 `/vision` 一件 angle_sensor 在视野内每 300ms 一条事件
  (hl-496749390 → hl-496749690 → …), 带速 0.0525m/s 单件视野停留 ~5.7s
  → **一件 ≈ 19 条识别**; 网关 uptime ~27h 累计 7519 条 vision 事件。
- **根因 (两段)**:
  1. `edge101/src/main.cpp hlTick()` I2C 轮询路径对每轮询结果**直接逐条发布**
     `sort/inbound/vision`, 无在视野去重 (`HL2_RETRIGGER_MS=500` 与
     `sort::HuskyLens` 的 retrigger 去重只用在旧 UART 路径, I2C 路径没用)。
     网关 `vision_dispatch_absence_s=0.8` 存在性去重只作用于**下发**分支
     (生产 `vision_dispatch=false`) → 记录侧完全不去重。
  2. 第一段加门控但阈值沿用 500ms: 门是"每物料 `last_` 每轮询都更新 →
     连续在视野只报一次; 复现间隔 ≥500ms 才视为新件"。**HL2 对薄/远工件
     丢跟踪间隙常 ≥500ms** → 同一件在 ~5.7s 停留内丢跟踪后复现即重报
     (实机 /vision 同物料事件间隔 3.9~4.5s, 含重报)。
- **修复**: `SortLogic/HuskyLens.h` 纯逻辑 `sort::RetrigGate` (按物料名);
  `hlTick()` 发布前过门; 阈值 `HL2_ABSENCE_MS=1500` (config.h):
  缺席 <1.5s 复现 = 同一件 (吸收 HL2 闪烁/丢跟踪), ≥1.5s = 新件。
  真实新件间隔 ≥3.5s (带速 52.5mm/s + 人工放料) 仍可区分;
  与网关 0.8s 下发去重配套 (双保险)。
- **单测**: 固件主机单测 **182/182** (`RetrigGate` 8 组断言: 6s 停留只报 1 次 /
  缺席 1.8s 新件再报 / 丢跟踪 0.9s 并回 / **边界 1.4s 并回 & 1.6s 再报** /
  多物料独立 / 空串 / reset)。
- **取舍 (已知, 非本轮引入)**: 键是物料名 (定制模型 129 ID 全 0), 两件**同物料**
  间隔 <1.5s 会合并成一件 — 与 F 节遗留 (密集同物料欠发) 同一取舍;
  彻底方案仍是位置式下发 (`pos_mm`), 与密集料串拨 (A) 配套。
- **验证 (2026-11-19 实机, 闭环)**:
  1. 第二段 TrackGate 标定值经 `HL2_RAW_DUMP=1` 旁路实采 (无摄像头, 全走 HL2 事件流):
     带跑 52.50mm/s 时单件轨迹 x 单调递减, 拟合 vx≈-210px/s → **PX_PER_MM=4.0**
     (640px 视野=160mm 带面), DIR_X=-1, y 恒定 (DIR_Y=0); 标定入 config.h
     (`HL2_TRACK_CALIBRATED=1`)。
  2. **标定后烧录曾引入 ×1000 单位 bug** (rate=px_per_mm×speed_mps×1000 → 210px/ms,
     正确 0.21): 带跑时每帧预测漂移 63000px≫tol → 每帧判新件。已修 (8aeca81),
     单测加第 11 组回归 **202/202**。
  3. **实机放料验证 (16:39–16:45, 4 种传感器 ~38 件)**: raw 轨迹逐帧回放 TrackGate
     仿真 = 47 上报; 网关 `/vision` 环 (maxlen=32) **满** = 32 条, 末 8 条间隔 1~60s
     (每件入射一次), 无"每 300ms 一条"风暴。用户实测确认无重复。
  4. `HL2_RAW_DUMP` 已回 0 (生产版, 本次提交)。
- **同轮附带修 (3D 孪生)**: `sortline_3d.html` device(真实)模式下 3D 推杆
  原来由**带面仿真件到达推杆位**自动触发 (视觉事件 spawn 的件 + 真实带运行
  → 件走到仓位 → 推杆动画), 而固件无指令不下发 → **3D 推杆自己动、真推杆不动**
  (用户报障)。修: 件到仓位自动推仅 sim 模式; device 模式推杆只认真实
  `/twin` motion 事件 (busy/ready), 无指令件直通带尾; device 模式不再显示
  仿真队列计数 (固件 FIFO 未上 MQTT)。headless Playwright (swiftshader +
  tick 手泵) 9 项断言全过 + sim 回归 2 项全过。

## 经验教训 / 踩坑记录 (2026-11-11 实机轮)

> 本轮 (队列+P到仓 / 推杆孪生联动 / 孪生上带验证) 踩过的坑与经验, 供后续复用。

### 密集料: p_mm 推序"看着乱"其实物理正确
- 9 连推序 (按 p_mm 升序) 如 1,4,2,5,7,3,6,8,9 看着不连续, 但**每件都进对仓**
  (1/4/7→仓1, 2/5/8→仓2, 3/6/9→仓3)。
- 根因: 到点时刻由 `p_mm = P(T0) + 仓距` 决定; 件 4→仓1(125mm) 到点 < 件 3→仓3(239mm) 到点,
  所以 4 号先推。这是**物理必然**, 不是 bug。
- 每次放件位置抖动 → p_mm 序会漂 (上轮 1,2,4,3,7,5,6,8,9 / 本轮 1,4,2,5,7,3,6,8,9), 属正常。
- **教训**: 判断对不对看"每件进对仓", 不要盯推杆动作的先后编号, 更别"修"推序。

### 密集料空推根因 (料距 <52mm)
- 仓距 1→2=62 / 2→3=52mm; 料距实测 8~10mm (PCB 宽 30-40mm) → 料比仓密。
- 推杆一伸把 2 件一起扫进仓 → 轮到第 2 件时仓口已空 → 空推; 固件"已推"计数偏多 (报 6 实际 4)。
- 解法见上方「已知待办 A」。

### 视频: cv2 写 mp4v 播不了 + 时间戳压缩
- cv2 `VideoWriter` 默认 fourcc=mp4v (MPEG-4 Part 2), 多数播放器不认 → **必须 ffmpeg 转 H.264**:
  `ffmpeg -i in.mp4 -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -movflags +faststart out.mp4`
- cv2 按 nominal FPS(=20) 写, 但实际产帧速率不同 → 视频比真实时间快/慢 (本轮实测 1.32× 压缩:
  328 帧/16.4s, 实际 ~21.6s)。**校准方法**: 每帧左上角烧 overlay 时间戳, 用播放位置反推真实时刻
  (播放 7.8s ≈ overlay t≈10.4s)。

### 环境: cv2 不在默认 python3
- 系统 `python3` 没 cv2 → `ModuleNotFoundError`。项目脚本一律用
  `/home/rockets/laya-industrial-bench/.venv/bin/python` (cv2/serial/paho/numpy 全有)。

### 孪生: 带面物料 = 视觉事件, 不是推杆事件
- 网页(真实源)带面上的物料**只由 HuskyLens 视觉事件上带**
  (固件 `[hl]` → `sort/inbound/vision` → 网关按 material_map 映射 yard → `/twin` → 网页 3s 窗口)。
- 手动 divert (MQTT) **不产生 vision 事件** → 带面空是**预期**, 不是网页 bug。
- 验证上带链路: 固件串口 `vision <material>` 注入模拟 (脚本 `/tmp/sim_vision.py`)。

### 孪生: 推杆联动只认真实 motion 事件
- SVG 推杆 busy(伸出)/ready(收回) 由真机 motion 事件驱动 (~1.5s 行程), 1/2/3 仓均验过。
- **裸命令 `servo <n> <deg>` 只动舵机、不发 motion 事件 → 网页不跟**。验联动要用真实推 (divert), 别用裸 servo。

### 自测/安全约束 (沿用)
- 推杆推时带子必须停; 队列每件推完自动 RMT 恢复带。所有起带命令必须带 `speed_mps` (0.0525)。
- C110 只允许一个进程开 `/dev/video0` → 录像从 MJPEG :8125 拉帧, 不直开相机。
- 双控制约束: 本机程序 / edge101 / sim 不能同时控同一台机; 真机测试不起 sim。

### 网页 SVG: 无 CDATA, 裸 && / < 非法 (曾整页动画崩, 2026-11-11)
- `edge101/docs/gate_layout_anim.svg` 内联 `<script>` **没有 CDATA**。直接写 JS 的 `&&` / `<`
  是**非法 XML** → 整个 SVG 解析失败 → **整页动画停 (网页传送带不动)**。
- 修法: 逻辑用嵌套 `if` / `>=` / `!==` 避开 `&&` 和 `<`; `&` 用实体 `&amp;`。
- **改完必验**: `python -c "import xml.dom.minidom as m;m.parse('...svg')"` (XML 合法) + `node --check`
  (内联脚本 JS 语法)。裸 `&&` 会让 XML 在**第一个 `&`** 处报 "not well-formed (invalid token)"。
- 手误多打引号 (如 `('busy'===gst')`) 会致字符串未闭合, XML 解析未必报, 靠 `node --check` 暴露。

### 网页: 手动 jog (job=manual) 别误触发 IR T0 / 光门 / 相机
- 点网页推杆按钮 = 手动 jog (motion 事件 `via=cmd`, `job_id=manual`), 但 SVG `twinFrame` 原把任何
  `busy` 都当 IR 触发 → 亮 IR T0 + 建 fake journey(上 item) + setVision(相机识别)。物理上 IR/视觉无事件, 纯网页误显。
- **区分标志 = motion 事件 `job_id==="manual"`**: 手动 jog 不亮 IR T0 / 不建 journey / 不显相机 / 不点光门, 只动画推杆;
  真分拣 (job!=manual, via=ir_due) 行为不变。`/twin` 事件字段名是 **`job_id`** (网关 `_on_motion` 构造)。

### 网页: 轮辐转速两模式不一致 (孪生太慢像静止)
- 循环演示 `render(t)` 用 `ang=(t*V)/16*57.2958` = 带速/轮半径 (**物理正确** ~501°/s)。
- 孪生 `twinFrame` 原 `beltT/1000*3.6` = **3.6°/s (100s/圈) → 肉眼=固定不动**。
- 修法: 孪生改 `((beltT/1000*VJ)/16*57.2958)%360` 与演示一致; 且每轮 1 辐条→4 辐条 (转起来像轮子而非针闪)。

### 网页视觉清理 (用户 2026-11-11)
- 光门 ×3 (虚线光束+传感器方块+到仓停推区/推杆标注) 移除; 仓库标注保留尺寸线区「仓 N · IR→推杆=dN」。
- 料道 ×3 的 marker-end 箭头 (gw-arwm 三角) + 料道线 (display:none) + 「1/2/3号料道」文字标注 都移除。
- **相机视场锥 (gla-fan) 保留** (用户一度以为是它, 澄清后指料道三角; 误删已恢复)。
- 移除被 JS `get()` 引用的元素: **保留元素加 `display:none`** (脚本设 opacity 无副作用/不报错), 比删元素+删 N 处 JS 稳。

### HuskyLens2 定制模型: 无真实 conf + Python -0.0 陷阱 (2026-11-13)
- **现象**: 定制模型(129)识别正常(4 种传感器全中), 但网关 `low_conf` 计数狂涨 → 物料全被拦, 不分拣。
- **根因 1 (固件侧, 无 conf)**: HuskyLens2 **旧固件** (Data 段 10 字节) **不上报 conf**。SDK 按
  V0.4 新布局把 offset 10-13 当 4 字节 float 读, 实际那 4 字节是 `name_length(1B) + 名字前 3 字节`,
  按 float 解释 = 次正规负数 (如 0x92A7E80F ≈ -4.5e-18), JSON 序列化后 ≈ **-0.0**。
  - 实测 4 种传感器: 超声波/人体红外/角度/温湿度, conf 全是次正规负数 (名字字节 MSB 恰好为 1)。
  - 证据: raw 包 offset 10 = 合法 name_length (15/21/18), offset 14 = 0xE4/0xE5/0xE6 (228+, 非法)。
    若按 V0.4 (conf 在 10-13, name_len 在 14) 则 offset 14 值全非法 → 证明实际是旧布局。
- **根因 2 (网关侧, -0.0 陷阱)**: 初版修 `if conf >= 0 and conf < confmin` 仍拦 → 因 **Python 里
  `-0.0 >= 0` 为 True** (IEEE 754: -0.0 == 0.0)。`conf < 0` 也拦不住 -0.0。
  - **必须用 `math.copysign(1, conf)` 区分符号**: copysign(-0.0)=-1, copysign(+0.0)=+1。
  - 最终判定: `math.copysign(1, conf) >= 0` 才走 confmin 过滤 (负含 -0.0 = 无有效 conf → 放行)。
- **协议 vs 实物**: DFR《HuskyLens2_Protocol_CN》V0.4 (2026-10-29) 明确 "Data 段 10→14 字节" (加 conf),
  且 RETURN_BLOCKS 表列 conf@offset10(4B)/name_len@offset14。但**手上这台 HuskyLens2 跑的是旧固件
  (10 字节, 无 conf)**, 协议文档描述的新布局它没实现。**别拿协议文档当实物布局, 以 raw 包实测为准。**
- **修法 (已落 sort_mqtt.py)**: 网关 conf 过滤前加 `math.copysign(1, conf) >= 0` 前置判断。
  内置算法 (conf 0~1 正数) 照常过滤, 定制模型 (conf 负含 -0.0) 直接放行。
- **遗留 / 有机会再更新**: 日后升级 HuskyLens2 固件到 V0.4 (14 字节, 有真 conf) 后, conf 变正数,
  此放行分支自动失效 (正常走 confmin)。届时可把该分支删掉 + 用真 conf 做质量阈值。

## 经验教训 / 踩坑记录 (2026-11-19 实机轮: HL2 去重闭环 + 3D 孪生)

> 本轮 (TrackGate 位置式去重 + 标定 + 3D 页) 踩过的坑与经验。

### 去重键设计: 名字/时间去重兜不住"连续帧上报", 连续帧要位置式
- 第一段 RetrigGate (1.5s, 按物料名) 只兜住**带停/慢速**场景 (件滞留视野复现);
  **带跑时每帧 HL2 都上报同一件** → 名字去重每帧都放 → 仍然重复。
- 终案 = TrackGate **位置式在视野去重**: 每件一个 `track_id`, 按
  `rate = PX_PER_MM × speed_mps` (4.0 px/mm × 0.0525 m/s = **0.21 px/ms**)
  预测 x, 容差 TOL_PX=60, ABSENCE_MS=600, kMax=16。一件一件轨迹, 报一次。
- **教训**: 去重键先问"同一件是怎么被反复上报的" — 上报有间隙 (闪烁/丢跟踪) 用时间/名字;
  连续帧上报用位置/track。两层并存 (固件 RetrigGate+TrackGate / 网关 0.8s 存在性) 是双保险, 不是冗余。

### 无摄像头也能标定: 用传感器自己的 raw 事件流当标定源
- 用户定案生产无摄像头 ("摄像头无非方便你自己分析")。TrackGate 标定全程不靠相机:
  `HL2_RAW_DUMP=1` 旁路发 `sort/line1/hl2/raw`, 带跑 52.50mm/s 下采**单件轨迹**
  (x 单调递减, 拟合 vx≈-210px/s → PX_PER_MM=4.0; 640px=160mm 带面; DIR_X=-1/DIR_Y=0),
  入 config.h `HL2_TRACK_CALIBRATED=1`。
- **教训**: 标定源 = 传感器原始事件 + 编码器带速, 相机只是旁证。标定完 raw 旁路关
  (`HL2_RAW_DUMP=0`, 生产版 d00effb), 用"raw topic 订阅静默"确认关闭。

### 单位 ×1000 bug: rate 类常数必须量纲分析 + 实机单件轨迹验证
- 标定烧录时误写 `rate = px_per_mm × speed_mps × 1000` → **210 px/ms** (正确 0.21)。
  症状: 带跑时**每帧**预测漂移 63000px ≫ tol → 每帧判新件, 看起来又是"一件识别十几个"。
- **教训**: ① px/ms = px/mm × mm/s ÷ 1000 — 换算写错一个量级, 症状是"每帧判新件"
  (漂移 ≫ tol) 而非"全判同一件", 容易误判成另一类 bug;
  ② 标定类改动**编译通过不算数**, 必须实机放件 + 单件 raw 轨迹逐帧回放验证;
  ③ 修完补单测第 11 组回归 (202/202) 锁死。

### 环形缓冲语义: count=32 是环条数, "显示 8 条"≠"总共 8 条"
- 网关 `/vision`: `deque(maxlen=32)` + API 只回 `[-8:]`。环满 count=32 = 实收 ≥32 条;
  曾误读为"本轮只收到 8 件"虚惊。
- **教训**: 回环形缓冲的遥测端点, 要同时看 count 与返回数组长度, 且查清截断逻辑;
  "末 N 条"接口上 count 才是真实累计口径。

### sim vs 实机对账: 验收标准 = 无重复风暴, 不是数量严格相等
- raw 逐帧回放 TrackGate sim = **47** 上报 vs 网关环满 **32**; 差额归因 QoS0 丢包
  + 设备侧 ~50% 帧丢 (raw 实测 80 帧/min, 连续订阅 10min 无断档 → 排除订阅侧)。
- **教训**: QoS0 + 传感器丢帧并存时数量严格相等本就不可能; 判定 = 每件 ≈1 条、
  无"每 300ms 一条"风暴、末 8 条间隔 1~60s、用户实机观察确认。
  帧率/吞吐一律**实测**, 不拿估算值 (当初 ~170 帧/min 的预估就是错的)。

### 3D 孪生: device 模式不许跑 sim 动画; 双向同步是高成本的
- device(真实)模式 3D 推杆原由"带面仿真件到达仓位"自动触发 → **3D 推杆自己动、真推杆不动**
  (固件无指令不下发)。修: 自动推仅 sim 模式; device 推杆只认真实 `/twin` motion (busy/ready)。
  (同主页面原则: 裸 servo 不发 motion → 网页不跟 — 只认真实事件。)
- 双向数据源同步 (3a97c40) 本轮**撤销** (64c133c): 3D 页 pill 不再反向控制主页面数据源,
  只保留新批次转发 → 数据源各页独立。
- **教训**: 只读孪生页做双向同步 = 状态漂移 + 回环风险, 收益低; 单向 + 新批次转发足够。
- 3D 页加了自动拟合包围盒相机 (整机居中); Break-It 幻影件演示随 bug 根治**删除** —
  演示条目和 bug 同生命周期, bug 修了演示就该撤, 否则误导。

### 3D 取景居中: 手算相机基向量 + 透视居中 (踩坑, 2026-11-19)
- **`wup.x` 符号**: 相机 up = right × forward, 其 x 分量是 `-sin(az)sin(el)` (非 `+`)。
  手算基向量写反 → y 投影被破坏 → fit 欠缩、底角出框 (`ndcMin.y≈-1.06`)。改对符号后 8 角全入框。
- **透视下 3D 盒心 ≠ 屏幕包围盒中心**: 只对 3D Box3 中心 lookAt, 投影后屏幕 bbox 仍偏
  (近大远小)。终案 = **Newton 居中补偿**: 用真实 `PerspectiveCamera` (同 fov/aspect) 投影 8 角点
  量出屏幕 bbox 中心偏移, 构造 2×2 Jacobian, 迭代平移 lookAt 目标 T 抵消 (≤6 次, clamp ±0.35,
  收敛 |offset|<0.01) → `ndcCenter=(0,0)`。
- **视锥/fit 深度必须用相对向量**: 判"角点是否在视锥内 / 求 fit 距离"时 `zP = d + (P−T)·fwd`,
  要减 lookAt 目标 T (相对向量); 误用绝对 `P·fwd` 会把目标位置偏移计入深度 → fit 距离错。
- **box-aware fit 取代 sphere-fit**: 8 角点精确投影求"最小完整框住"距离 (margin 1.1),
  长条形机体不会被球拟合切边。终值: 设备占 ~74% 帧高, `ndcCenter=(0,0)`, 8 角全 in-frame。
- **教训**: 手算相机/坐标变换后, 验证一律用真实 `camera.project()` 的 NDC 数值, 别信手算;
  "3D 居中" ≠ "屏幕 bbox 居中", 透视下二者不等, 要分两件事做 (fit 距离 + Newton 居中)。

### 无头验证三坑 (headless Chromium + three.js, 2026-11-19)
- three.js canvas `preserveDrawingBuffer:false` → 渲染循环外读 `readPixels` **全零**;
  验证渲染用 **screenshot 像素统计** (非黑占比) + 页面自带调试钩子 (如 `__s3d.state()`),
  别用 readPixels。
- 字体子集化页面 `waitUntil:'load'` 可能永不触发 → `domcontentloaded` + 显式等待。
- 浏览器版本漂移: playwright 1.65.1 期望 headless shell 1255, 本机缓存只有 chromium-1248
  → `chromium.launch({ executablePath: .../chromium-1248/chrome-linux64/chrome })`。
- 起浏览器前 `pgrep -fa chromium` 查现有实例; 任务结束必关, 不留常驻 (本机 8 核, 浏览器是 CPU 大头)。
- favicon 404 无害, 别追。

### 无头验证脚本: 自己注入的 pump 代码会假报"页面异常"
- 脚本用 `addScriptToEvaluateOnNewDocument` 注入 rAF 泵, 泵里写了 `++(window.__x = ...)`
  (前缀自增作用在赋值表达式上) → 该表达式是**非法 LHS**, 注入代码在新文档里解析即抛
  `SyntaxError: Invalid left-hand side expression in prefix operation`, 被脚本自己的
  `exceptionThrown` 监听当成"页面错误"记下来 → **误判页面有语法错误** (页面其实干净)。
- **排查**: 报"页面 SyntaxError"先 grep 验证脚本自己注入/eval 的代码 (泵、探针、调试钩子);
  页面真实干净与否另用一个**不自注入**的独立 load test 复核; 模块 JS 是否真无语法错,
  用 awk 抽出 `<script type="module">` 内容后 `node --check` 判最准。
- **教训**: 前缀自增 `++`/`--` 只能作用于**变量/属性引用**, 不能作用于 `(a = b)` 这类表达式;
  验证脚本自身的 bug 会以"页面错误"形态混进遥测, 先分清错误来源再下结论。

## 2026-10-30 修复记录 (用户提交的 5 个问题)

| # | 问题 | 修法 | 验证 |
|---|---|---|---|
| ① | 网关 `ack_timeout_s=3.0` 与固件 divert 时长不匹配 (qty≥4 必超时 → 重发撞 busy → 故障锁存 + 账实差 + 可能双拨) | 三重修复: (a) 网关 divert 超时随 qty 缩放 `3s + 1s×qty`; (b) busy-ack 绝不重发、只延长等待 (busy_wait_s×max_ext); fault-ack 立即失败; (c) 固件按 job_id 去重 (完成窗 60s 内同 job_id 重发只补 ok 不重拨) | E2E 第 8 节 6 项回归 (qty=4 实机时序成功且未重复拨 / 慢设备 busy 延长仍完成且只拨 1 件); 主机单测 66 项 |
| ①-附带 | 传送带 "stop 已停" 回 busy → 网关按 busy 语义空等 9s | 幂等目标态语义: sim + 固件 Conveyor 改为 "已处于目标态回 ok", busy 只表示正在执行 | E2E 全绿 + 主机单测 Conveyor 新增 8 项 |
| ② | 主循环 digitalWrite 打 STEP 仅 ~3kHz (名义 320k pps) | RMT TX 硬件脉冲: 1 个 [pulse,gap] 符号 × loop_count=-1 无限循环, APB 80MHz 精确 50% 占空比; 主循环只做 start/stop 看门; 创建失败自动回退旧路径 | `pio run -e edge101/s3emu` 均 SUCCESS; 实机验证: 接电机后按 1m/s 跑 10s 量带速 (OPEN #6⑧) |
| ③ | loop_ui /dispatch 把网关 202 压成 200 | `JSONResponse(..., status_code=r.status)` 透传 | TestClient + stub 网关: 202/409/422 全透传 |
| ④ | 契约字段漂移 (ts 全 ""; steps_total vs items_in) | 契约明确: `state` 必填, 其余设备可选; `steps_total`/`items_in` 不同量纲并列; `ts` 无 RTC 可 `""` (网关忽略, 后续 NTP) | docs/sort-mqtt.md 命令/应答/遥测三节重写, 与代码行为一致 |
| ⑤ | STEPS_PER_MM / 物料映射是假设 | **STEPS_PER_MM 实测定稿 69.1 (2026-11-03, 三次卷尺+基准加权 13400÷193.82)**; 物料映射仍 OPEN #1 | — |
| ⑥ | 拨料时序: divert 收命令即执行, 物品位置无处安放 (分拣器对位置一无所知) | **光门触发** (方案 C): divert `trigger=photo` → 分拣器 `armed`, 推杆**前侧 (上游)** 光门落沿才推 (安装距离 = 带速×(动作时间+余量), 公式入 docs/sort-mqtt.md「光门时序」); qty>1 每件各等一次光门; 5s 无物品 → fault "photo timeout" + job_id 回 ack; 视觉坐标 x/y/w/h + pos_mm 带面位置随批次事件/台账透传 (方案 B) | 主机单测 104 项 (Gate 边沿去抖 + Sorter photo 全路径); E2E 第 9 节 7 项 (photo 触发拨出 / trigger 字段 / armed 状态 / 坐标透传 / photo 超时 fault 锁存 / 清除恢复); BOM 新增 光门 ×3 (OPEN #6⑨) |
| ⑦ | 光门未安装 (BOM ⑨ OPEN) 时 photo 时序无法成立 — 实机只能 immediate, 与 `gate_layout.svg` 时序不符; 且设备运动无遥测, 网页示意图无法反映实际 | **虚拟光门 + 运动事件时间线**: 无物理光门时按「HL2 检测锚点 (像素→带面位置) + 带速匀速外推」预测物品过光门线时刻, 到点推杆 (固件 `Track.h`, 纯逻辑可单测); 解析顺序 物理光门 → 虚拟光门 → immediate; 固件每次分拣器状态迁移广播 `sort/line1/motion` (via=immediate/photo/virtual/...), 网关环形缓冲 + relay `sort/event/motion` + `GET /motion`; 视觉事件带 `pos_mm` | 主机单测 141 项 (Track 位置跟踪/预测 + Sorter 虚拟光门全路径 + 协议); E2E 52 项 (第 10 节 8 项: 时间线顺序 via=photo/relay/缓冲/HTTP/pos_mm/取消/injected); `pio run` 双环境 SUCCESS; 文档: sort-mqtt.md 虚拟光门节 + wiring.md 标定方法 + README×2 |

同时: sim_devices 改为按件计时 (默认 900ms/件, 全拨完才回 ok, busy 守卫),
E2E 从 31 项扩到 37 项 (第 8 节实机时序回归)。

## 本次已完成 (过夜自动推进)

| 项 | 说明 |
|---|---|
| `vision_input.py` | 本机视觉接入 (HSV 4 色块): 帧 → 发 `sort/inbound/vision`。外部带模型视觉传感器 (含 HuskyLens 2) 可跳过本模块直发同一 topic |
| `tests/test_sort_mqtt.py` | 网关 E2E 31 项全通过 (进程内 amqtt broker + 模拟设备 + 网关) |
| `sort_mqtt.py` 修复 ×3 | ① paho 2.x 非法通配符致订阅线程崩溃 ② 遥测正则不匹配裸 topic ③ paused 后按遥测 start/resume |
| `loop_ui` 下发按钮 | 「📤 下发分拣线」: Laya 分类结果 → `POST /dispatch` → 网关 :8123 (手动触发, 不自动) |
| `docs/sort-mqtt.md` | 控制器接入协议契约 (命令/应答/遥测/白名单/统计/安全降级) |
| **EDGE 101 固件** `edge101/` | 按 DFRobot wiki DFR1283 实测规格实现: 板载 RJ45 静态 IP + HL2 UART (GPIO5/12 重映射 Serial2) + 步进 (STEP=14 DIR=15, EN=MCP23017 PA0) + PCA9685 I2C 3 舵机 (SDA=18 SCL=23) + MCP23017 I2C 数字量 (EN+预留) + 2Hz 遥测; 行为与 sim_devices 等价, 对网关零改动 |
| 固件主机单测 | `edge101/tests/host/` 60 项全绿: Conveyor/Sorter 状态机对拍、HL2 帧解析 (断片/坏校验/垃圾字节恢复)、进画面去重 (500ms 滑动窗)、协议 JSON/topic、MCP23017 步进 EN (在线/离线/状态映射) |
| 实机闭环验证 | 全链路 (broker+模拟设备+网关+视觉) 手工跑通; 视觉 371ms, 语音 168ms, 7 命令 7 应答 0 超时 |
| 实测踩坑记录 | ESP32 UART 解析器两个 bug (非 0x55 数据字节被误当失同步 / 校验和越界读) 已修并以单测锁定; busy/fault 应答语义与 sim 对齐 (busy 也发 ack) |

## 未决 (需要结论)

### 物料与识别

1. **物料 → 分拣器映射** — 物料集与路由方式**已定案 (2026-11-13)**, 具体仓号仍是占位
   - 生产线物料集 = **4 传感器** (用户定案: 真实线不用水果/方块);
   - **主路由 = 语音指令** (网关指令需求台账, 见上方 2026-11-13 完成节);
   - `config/material_map.json` 仅兜底, 具体仓号是**测试占位** (工作区 2026-11-13
     的改动只是测试用, 非现场定案; 提交版 ultrasonic→2 同样不作数); 现场定案待做。
   - 固件侧 HL2 识别名直接是物料 key (定制模型按名, ID 全 0)。
2. ~~**水果识别模型**~~ **已解决 (2026-11-13)**: 物料集已切 4 传感器, HuskyLens2 用**定制模型**
   (algo=129 YOLO, 按名识别, 4 种全中); 水果仅留在 sim 演示域 (独立工具 bench_fruit 等),
   不再走视觉自动分拣闭环。
3. **步进位置对账** (驱动器已定 MKS SERVO42C 闭环, 见第 6 项)
   FOC 闭环 + 堵转保护已覆盖丢步/堵转; 剩下可选项:
   ① 驱动器"单圈内上电自动回零"启用哪种 (两模式之一)?
   ② 料道末端加光电开关做长期对账 (超阈值报警)? *影响: 统计可信度。*
      (MCP23017 上 15 路预留数字 IO 可直接接, 加接线 + 固件输入读取即可)

### 硬件采购与接线 (新, 固件按以下假设写的, 不符就改 config.h)

4. **舵机 ×3 (驱动推杆)**: 假设舵机 0–90° 扫掠 → 推杆推出行程、单次拨一件、
   动作时间 <400ms。实际舵机型号/扭矩/时间? **推杆机构**的几何
   (推杆伸出行程够不够把件推进料道? 与带面/料道夹角?)。
   **接线 (2026-10-30 起已定)**: 信号直连 GPIO12/15/33 (LEDC PWM), 不采购 PCA9685。
5. **PCA9685 模块**: ~~要采购 1 个~~ (2026-10-30 取消: 舵机改 LEDC 直连,
   7 路输出 GPIO 5/12/14/15/18/23/33 全用满, I2C 18/23 留给 HL2)。
6. **步进电机与驱动器** (已定: **MKS SERVO42C 闭环 PCBA** + 42 两相电机,
   文档: CSDN 120565771 — 16384 线磁编码器 + FOC 闭环):
   ① 电机型号/导程/额定电压电流? (**STEPS_PER_MM 实测定稿 69.1**
      = 13400步÷193.82mm 加权, 三次卷尺+基准, 2026-11-03; 细分/导程仍未确认)
   ② 驱动器细分设多少? (与 ① 一起决定 STEPS_PER_MM; 出厂默认值?)
   ③ 堵转保护 / 自动回零 / 熄屏 默认开哪些?
   ④ 电源: 驱动器与电机共 12V 一路够吗 (12V/1.5A+)?
   ⑤ EN: **不接线** (2026-11-03 实机裁决: SERVO42C 低电平使能 — 浮空=使能,
      高电平=禁能; 固件不控 EN, 皮带 3200 步实测转动。想固化可驱动器端跳 GND)。
      `McpEn.h kActiveLow` 已改 true; 未来装 MCP23017 时 PA0 接管
      (running=使能 EN 低, stopped=禁能 EN 高)。
   ⑥ 磁环安装 (轴中间/水平) 与线序 A+A-B+B- 按文档第 2 部分做, 屏显
      "Phase Line Error!" 时断电重调。
   ⑦ 采购: **MCP23017 (DFR0626) ×1** — 5V 供电, 拨码全 1 (地址 0x27),
      **PB0/PB1/PB2 → 3 路光门 (新, ⑨)**; PA0 预留 (驱动器 EN, 现不接线);
      余 13 路预留数字量 (末端对账光电/限位/急停)。
   ⑧ **带速实测反推 STEPS_PER_MM** — **已完成 (2026-11-03)**: 卷尺+基准
      三次 (RMT 5kHz 手动脉冲): 3200步=46.6mm / 3200步=48.11mm /
      7000步=99.11mm (±3%) → 加权 13400÷193.82 = `STEPS_PER_MM=69.1`。
      双向闭环验证: 正向 10200 步=147.22mm (69.3步/mm) vs 反向 10140 步
      回零 (净偏差 0.87mm, 目视到位) → 正反向一致, 无显著背隙/丢步。
      后续跑带按 1.0m/s = 69100 pps; 若确认细分/导程(①②)后偏差 >2%,
      按公式重算再测复核。
   ⑨ **光门 ×3 (新, 2026-10-30, 推杆推出时序触发, divert trigger=photo)**:
      透过式光电开关, **NPN 开集**输出, 5V, 如 E3T-ST61 或同级
      (必须 NPN 开集 — PNP 源型与 MCP23017 内部上拉不配)。每推杆 1 路,
      装推杆**前侧 (上游)**: 距推料点 = 带速 × (推出动作时间 + 余量)
      (公式与标定: docs/sort-mqtt.md「光门时序」)。OUT → MCP23017
      PB0/PB1/PB2 (内部上拉, 挡光=LOW 落沿=物品到达)。装好后把网关
      `config/sort_gateway.json` 的 `divert_trigger` 改 `"photo"`。
      **可选项 (2026-10-30 起)**: 虚拟光门已就绪 — 未装光门时固件按
      HL2 检测锚点 + 带速预测过光门线时刻 (`divert_trigger=photo` 即可,
      固件自动走虚拟光门), 网页动画时序照样成立; 光门仍建议安装
      (实测精度 > 预测, 且带速波动时预测会漂移)。
      接线调试: 串口 `gates` / `track`。件距约束 ≥ 带速 × 0.9s。
      **需随装标定** (卷尺量, 改 config.h): `GATE_DIST_MM[3]` 相机参考平面→
      各光门距离 / `VIEW_WIDTH_MM` 相机视野宽度 / `ITEM_PITCH_MM` photo job 件距
      (方法: edge101/docs/wiring.md「虚拟光门标定」)。
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
- 固件编译: ESP32 交叉编译已跑通 (pio run SUCCESS, 2026-11-03, 新接线:
  舵机 LEDC 直连 12/15/33 + 步进 STEP=14/DIR=5/EN 不接线 + RMT 手动脉冲;
  实机: 舵机扫掠 + 皮带 3200 步转动均摄像头验证通过); 主机单测
  `./edge101/tests/host/run_tests.sh` 172/172 全绿; E2E **77/77** 全绿 (2026-11-13,
  含指令路由 §3b)。
