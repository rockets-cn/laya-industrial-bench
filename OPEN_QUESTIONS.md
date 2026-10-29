# 分拣线接入 · 未决问题 (待讨论)

> 2026-10-29 夜。以下为自动推进时**没有结论、需要拍板**的部分；
> 已实现内容见 `docs/sort-mqtt.md` 与下方「本次已完成」。

## 本次已完成 (过夜自动推进)

| 项 | 说明 |
|---|---|
| `vision_input.py` | 视觉传感器接入: 帧 (合成/图片/摄像头) → HSV 像素层识别 4 色方块 → 发 `sort/inbound/vision` (material + confidence + frame_id)。外部带模型的视觉传感器可跳过本模块直发同一 topic |
| `tests/test_sort_mqtt.py` | 网关 E2E 31 项全通过 (进程内 amqtt broker + 模拟设备 + 网关): 批次下发/故障锁存恢复/视觉入站/SCADA 入站/白名单拒绝/统计 summary/视觉传感器 E2E |
| `sort_mqtt.py` 修复 ×3 | ① paho-mqtt 2.x 下 `sort/sorter+/...` 非法通配符导致订阅线程崩溃 (网关静默失聪) ② 遥测正则不匹配裸 telemetry topic (网关从不更新设备状态) ③ 传送带 paused 后只发 start 必失败 → 改为按遥测状态 start/resume |
| `loop_ui` 下发按钮 | Web 控制台新增「📤 下发分拣线」: Laya 分类结果 (yard 域 actions) → `POST /dispatch` → 网关 :8123。需人工点击, 不自动下发 |
| `docs/sort-mqtt.md` | 控制器 (PLC) 接入协议契约: 命令/应答/遥测 topic、状态机、时限、白名单、统计 topic、安全降级、接入清单 |
| 实测 | 全链路 (broker+模拟设备+网关+视觉) 手工闭环跑通, 统计/台账/summary 正常 (见会话记录) |

## 未决 (需要结论)

1. **物料 → 分拣器映射**
   `config/material_map.json` 当前是**占位** (4 水果→1 号, 红/绿块→2 号, 黄/蓝块→3 号)。
   现场每个分拣器料道实际放什么物料? 还是用 `round_robin` 轮转?
   *影响: 视觉识别后发到哪条料道。语音路径的 yard 号由指令决定, 不受此表影响。*

2. **水果的视觉识别方案**
   本机 (CPU) 没有离线 CV 模型能分 `red_apple/green_apple/banana/pear`; HSV 只能覆盖
   4 种颜色方块 (色块靠色相, 水果靠形状/纹理, 阈值法不可靠)。选项:
   - (a) 外部视觉传感器自带模型, 直接发 `sort/inbound/vision` (协议已就绪, **推荐**)
   - (b) 后续训一个小模型 (YOLO/轻量 CNN) 放 `vision_input.py` 第三路径 — 需要样本与时间
   - (c) 水果只走语音/文本路径, 视觉只管方块
   *影响: 哪些物料能进「视觉自动分拣」闭环。*

3. **摄像头硬件**
   型号 / USB 索引 / 分辨率 / 安装位置 / 补光? `vision_input.py --camera N` 接口已就绪,
   但 `frac_min` (0.08) / `dom_ratio` (2.0) 是合成图调的, 实机需要对着真实画面重调。
   *影响: 识别率与误报率。*

4. **视觉防重复 (ghosting)**
   同一件物料在画面里停留多帧 → 当前每帧发一个事件 → **同一件会被重复分拣**。
   候选: (a) 物理触发 (光电开关/编码器过门时才识别, 最可靠) (b) 时间窗去重
   (同物料 N ms 内只算一件) (c) 网关侧加「单件过门」门控。
   当前**没有**去重逻辑, 实机接入前必须定。*影响: 重复分拣/统计虚高。*

5. **divert 与 qty 语义**
   语音路径一条命令带 `qty=N` (PLC 自行完成 N 件); 视觉路径一帧一件 (qty=1)。
   若真实分拣器是「每次触发单件拨出」, 语音路径应在网关展开成 N 条 qty=1 命令,
   还是 PLC 内部数件? *影响: 命令协议实现方。*

6. **控制器 (PLC) 细节**
   PLC 型号? 原生支持 MQTT 吗 (还是要网关转发)? 是否需要 broker 认证
   (用户名/密码/TLS) — **当前网关 paho 客户端没有认证配置, 生产 broker 需要的话要加**。
   生产 broker 用 mosquitto (需 root 安装) 还是继续 amqtt (纯 Python, 无特权)?
   *影响: 上线前配置项。*

7. **自动下发开关**
   当前 `loop_ui` 只出分类结果 + 人工点「📤 下发分拣线」(安全优先)。
   要不要 `/run` 验证通过 (FAST/EXPAND) 后**自动**下发?
   *风险: 语音误识别直接落到物理分拣 — 建议保持手动, 至少加二次确认。*

8. **批间停带策略**
   `stop_conveyor_after_batch: true` (每批结束停带, 下批再 start)。
   连续生产场景要不要常开 (只故障时停)? *影响: 节拍与 E2E 延迟 (每批多 1 次 start 往返)。*

9. **统计留存**
   当前: 内存 + JSONL 台账 (`stats/sort_ledger.jsonl`, 每 job 一行) + retain summary。
   量级够不够? 要不要转 SQLite/InfluxDB? *影响: 长期对账与报表。*

10. **service 化**
    broker/sim/gateway 目前手动起 (测试自包含)。要不要加 systemd 用户服务
    (`deploy/laya-sort-gateway.service` 等, 与现有三个服务同风格)?
    *影响: 开机自启。sim_devices 上线时应去掉, 只留 broker + gateway + vision。*
