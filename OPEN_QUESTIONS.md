# 分拣线接入 · 未决问题 (待讨论)

> 2026-10-30 更新。原 2026-10-29 夜自动推进时**没有结论、需要拍板**的部分保留在下方;
> 已实现内容见 `docs/sort-mqtt.md`、`edge101/`。

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
- **待做**: HuskyLens 自学习模型认出蓝旋钮 PCB — `edge101/include/config.h` 的
  `MATERIAL_MAP {6,1,"pcb"}` 是占位 (注释"训练后确认 id/仓"), 需确认实际 object id + 门槛 conf≥0.6;
  或评估切物体追踪模式 (当前用户在追踪模式, 能识别 1 物体, 但非最终自学习分类效果)。

### C. 薄/透光物体 IR 不触发
- **现象**: 薄/透光 PCB 在带面上, 单点红外接近 (GPIO34) 偶发不触发 T0。
- **待做**: 调 IR 灵敏度 / 加兜底 (视觉事件 + 带速外推补 T0) 或换传感器。未量化。

### D. 3 号仓精度 ±3mm
- **现象**: 3 号仓偶发上游 3mm (偏早)。根因未定 (物体放置抖动 vs IR 触点 vs 推杆行程)。
- **待做**: 标定复核或加末端对账。

### E. 全链路上机验证未完整
- **未跑**: 语音 → ASR → laya 决策入口; 网关 `POST /dispatch` 真流程 (本轮均为手动 MQTT/串口触发)。
- **待做**: 上实体后跑一次全链路回归。

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

1. **物料 → 分拣器映射** (原问题, 现在有两处要同步改)
   - 网关侧 `config/material_map.json` (语音路径 yard 判定 + 视觉路径料道选择)
   - 固件侧 `edge101/include/config.h` `MATERIAL_MAP` (HL2 {type,ID} → 物料名)
   现场每个料道实际放什么物料? 还是 round_robin? 当前两边都是占位。
2. **水果识别模型** (用户: "后续交给 huskylens2, 还没有想好")
   协议已就绪 (type=5 自定义模型 ID → 物料名), 剩下要定:
   用 HL2 自带基础分类还是导入 YOLO? 水果类别表? 训练样本谁出?
   *影响: 水果能否进视觉自动分拣闭环; 不定的话水果只走语音路径。*
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
  `./edge101/tests/host/run_tests.sh` 172/172 全绿; E2E 52/52 全绿。
