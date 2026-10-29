# 语音下单决策链路测试报告 (Laya / qwen3.5 / Whisper)

> 日期: 2026-10-08 ~ 10-09
> 环境: CPU only (15 GB RAM), Laya via `jev_api.py` (:8000), Ollama (:11434), faster-whisper
> 场景: 语音/文本订单 → 判定 {红苹果, 绿苹果, 黄香蕉, 梨} × 数量 × 是否要

---

## TL;DR

| 方案 | 语音 E2E | 意图准确率 | 实时路径含 LLM | 可复现 | LLM 挂掉时 |
|---|---|---|---|---|---|
| 纯 Laya (rich criteria) | ~0.3 s | 71.4% (描述) / 58% (多意图) | 无 | ✅ | 正常 |
| LLM 前置 (qwen3.5:4b) | 6.5 s | 100% (意图) / 95% (描述) | **每次** | ❌ | 链路瘫痪 |
| **工业回路 (最终形态)** | **1.53 s** | **100% (40/40)** | **仅旁路** | ✅ | 降级暂停+人工 |

**结论**: Laya 是"分类器"不是"推理器"——裸关键词场景 100% 且 100ms 级,
但否定句、多意图、纯特征描述是硬伤且错时置信度虚高(0.94~1.00)。
正确架构是 **ASR → 规则归一化 → Laya 结构化决策 → 确定性校验器 → PLC**,
LLM 只放离线侧和旁路。详见 §4。

---

## 1. 测试脚本

| 脚本 | 用途 | 运行 |
|---|---|---|
| `bench_fruit.py` | 21 条水果描述分类 × N 轮, criteria 可切换 | `RICH=0\|1 .venv/bin/python bench_fruit.py 10` |
| `bench_split.py` | 单请求多问题 vs 逐意图拆分 A/B | `.venv/bin/python bench_split.py 10` |
| `bench_pipeline.py` | LLM 意图整理 → Laya 终审 | `LLM_MODEL=qwen3.5:4b .venv/bin/python bench_pipeline.py 10 3` |
| `bench_voice.py` | 语音 → ASR → LLM → Laya | `.venv/bin/python bench_voice.py 3` |
| `bench_industrial.py` | **工业回路** (最终形态) | `.venv/bin/python bench_industrial.py 5 voice\|text` |
| `teacher_offline.py` | **离线教师**: LLM 造"语音文本→意图"语料 + 交叉标注 → 微调 Laya (设计 ①) | `.venv/bin/python teacher_offline.py all` |
| `run_all.sh` | 依赖就绪后自动跑 pipeline + voice (带锁) | `LLM_MODEL=qwen3.5:2b ./run_all.sh` |

依赖: `laya` (models/laya 已就位), `qwen3.5:4b`/`qwen3.5:2b` (ollama),
`models/whisper-small` (modelscope: pengzhendong/faster-whisper-small),
`espeak-ng` (pacman), `edge-tts` (pip, 生成自然测试语音 `test_audio/tts_1..4.wav`)。

---

## 2. 纯 Laya 基线

### 2.1 描述分类 (`bench_fruit.py`, 21 条 × 10 轮)

| criteria | 总体 | 红苹果 | 绿苹果 | 香蕉 | 梨 | 延迟 (mean/median/p95) |
|---|---|---|---|---|---|---|
| BASE (简短) | 66.7% | 100% | 100% | **20%** | 40% | 269 / 144 / 502 ms |
| RICH (特征强化) | **71.4%** | 83% | 100% | 20% | **80%** | 312 / 216 / 511 ms |

发现:
- 描述里**出现水果名** → 100%; 只给特征不给名字 → 香蕉/梨崩
- 强化 criteria 对"形状判别"(梨: 上窄下宽)有效, 对香蕉无效, 还误伤 1 条红苹果
- **确定性**: 每条用例要么 10/10 全对要么 0/10 全错 (逐位可复现)
- **校准差**: 错判时 choice 概率可达 1.00 (case: "长条弯曲剥皮"→红苹果, p=0.99~1.00)
  → **不能靠置信度兜底**

### 2.2 多意图订单句 (`bench_split.py`, 4 句 × 10 轮, 3 个 noul 问题)

| 方案 | 单句延迟 | 句级全对 | 问题级准确 |
|---|---|---|---|
| 单请求多问题 | **108 ms** | 25% | 58.3% |
| 逐意图拆分 (3 请求) | 163 ms | 25% | 58.3% |

- 两方案答案**逐位相同** → 不存在"多问题稀释", 拆分无收益 → 用单请求
- 错误根源: ① 否定句 ("不要梨" → noul 0.943, 方向反了) ② 梨/香蕉权重系统性偏低

### 2.3 能力边界 (意图判断能否用 Laya?)

| 能做 (可用) | 不能做 (实测证伪) |
|---|---|
| 单意图 + 关键词直说 (100%, 100~300ms) | 否定语义 (方向会反) |
| 简单多分类/路由 (基准套件 90%+) | 多意图并列 (58%, 且拆请求无效) |
| 带数量的意图识别 (0.92) | 纯特征推理 (香蕉 20%) |
| 工程判据: 输入短/意图单一/措辞规范/类别区分度高 | 置信度不可信 (0.94~1.00 也会错) |

**一句话: 意图"分类"可以, 意图"理解"不行。**

---

## 3. LLM 方案 (qwen3.5)

### 3.1 文本流水线 (`bench_pipeline.py`, LLM 整理 → Laya 终审)

| 指标 | 纯 Laya | LLM 流水线 |
|---|---|---|
| want 判断 | 58% | **100% (90/90)** (否定句全对) |
| 数量提取 | 无数字能力 | **100% (70/70)** |
| 描述分类 (21 条) | 71.4% | **95%** (唯一错: 中文梨特征句→香蕉) |
| E2E | 108 ms | 5.5 s (CPU, LLM 占 5.1s) |

- **Laya 终审是负贡献** (67%): LLM 已给出"香蕉", Laya 归 4 类时又判回红苹果
  → 该任务 LLM 一步到位, Laya 终审应砍掉
- qwen3.5 是思考模型, ollama 请求必须带 `"think": false`, 否则 token 预算被
  thinking 吃光, response 为空 (排查过, 见 §5 坑位)
- 4b vs 2b: 4b 意图 100%、描述 95%; 2b 已部署 (`qwen3.5:2b`, 2.7GB) 供旁路使用

### 3.2 语音全链路 (`bench_voice.py`, ASR→LLM→Laya)

| 阶段 | 耗时 | 备注 |
|---|---|---|
| ASR (whisper-small CPU, edge-tts 自然语音) | ~1.1 s | 同音字错: 梨→離/离; "十个"正确 |
| LLM 整理 | ~5 s | **消化 ASR 同音字错误, 意图 3/3 × 9 次全对** |
| E2E | **6.5 s** (p95 7.6 s) | |

- espeak-ng 机器音方案不可用 (ASR 匹配率 7~59%, 意图 0/12)
- edge-tts (zh-CN-XiaoxiaoNeural) 后 ASR 匹配率 59~96%, 且 LLM 纠错能力补足

---

## 4. 工业回路 (最终形态, `bench_industrial.py`)

```
语音 ──► ASR ──► 规则归一化 ──► Laya 结构化决策 ──► 确定性校验器 ──► PLC 下发
              (whisper-small)   (简繁/同音字/否定/     (4 动作白名单,    (干跑)
                                 数量抽取)              交叉检查, 越界拒绝)
                                          │ 拒绝
                                          ▼
                                   LLM 旁路 (qwen3.5:2b, think=false)
                                          │ 输出过同一个校验器 + 幻觉防护
                                          ▼ 仍失败
                                   PAUSE + 人工确认 (默认安全降级)
```

### 实测 (5 轮 × 4 句 × {voice, text} = 40 次)

| 模式 | E2E mean | p95 | 路径分布 | 正确率 |
|---|---|---|---|---|
| 语音 | **1.53 s** | 1.98 s | FAST 20/20, 旁路 0, 暂停 0 | **100%** |
| 文本 | **227 ms** | 469 ms | FAST 20/20 | **100%** |

分段 (语音): ASR 1284 ms + Laya 241 ms (max 412 ms)。

### 超纲/异常输入 (安全降级验证)

| 输入 | 路径 | 说明 |
|---|---|---|
| "把三号口的窗户关上" | PAUSE+HUMAN | 领域外, 空订单 |
| "不要香蕉" | PAUSE+HUMAN | 纯否定无订单 (LLM 幻觉项被防护拦截) |
| "给我来十个苹果" | **FAST** | 单意图 100ms 级, 数量正确 |

### 关键工程点 (踩坑后沉淀)

1. **Laya 问法工程**: 多问题塞一个请求时裸词"苹果"漂成香蕉 (0.64);
   **每提及单独请求 + state 写成 `用户要货: 梨 x 3`** → 全部 conf 1.0。
   快路径 100% 靠这个。
2. **校验器真拦事**: 规则映射 vs Laya 决策交叉检查, Laya 漂移时 100% 触发旁路, 零漏放。
3. **LLM 会幻觉**: 纯否定句让 qwen 编出整单 3 项 → 加了**幻觉防护**
   (LLM 只能下单规则层听到过的词)。LLM 输出必须过同一个校验器。
4. **whisper 惰性生成器**: `transcribe()` 返回生成器, 计时必须在
   `"".join(segs)` 消费之后, 否则 ASR 计时少算 ~1s。
5. **ollama 思考模型**: 必须 `"think": false`; 并发多客户端会互相拖死
   (实测 3 个重复 runner 把 ollama 拖到 0.2% CPU 假死) → `run_all.sh` 带锁。

### 交互方式经验 (2026-10): 按键说话 (PTT) 优于 唤醒词+自动断句

Web 控制台 (`loop_ui.py`) 从「唤醒词「优优」+ 自动断句 (SILENCE=1.2s)」改成
「🎤 授权 / 🗣 按住说话 / ⏺ 录音识别」三控件, 唤醒词降为可选开关 (默认关)。
用户实测反馈: 响应「快了很多」。拆开看, 快在三处:

1. **省掉句尾静音等待 (大头, 恒定 ~1.2s)**: 自动断句必须在句尾静默
   SILENCE=1200 ms 后才触发 flush — 每句指令都背这 1.2 s 尾部延迟;
   PTT 松手即 flush, 这段感知延迟直接归零。
2. **省掉唤醒词往返**: 旧模式先 ASR 唤醒词 (~100–250 ms) + 600 ms 唤醒窗口
   回到 idle, 还要多说一句; PTT 零往返。
3. **片段更短更干净**: ASR 耗时随音频长度近线性 (本机实测 SenseVoice 常驻:
   4.2 s 片段 mean 63 ms vs 2.5 s 片段 44 ms); PTT 片段只含指令本身,
   无唤醒词/前后噪声, 更短更快。副产物: 录音边界由人显式控制,
   「下一句混进上一句」的问题 (原 "识别中不录音" 补丁治的病) 基本消失。

落地: 录音统一走 `flushWav` (峰值归一 + 16k mono WAV), 指令请求带当前
domain — 原先麦克风路径不带 domain, 恒按水果域解析, 顺手修掉。
**结论**: 实时工业语音交互, 「显式结束」(按住说话 / 双按钮切换) 优于
「自动断句 + 唤醒词」; 唤醒词留给免提场景, 做成可选开关而非默认。

### 分配指令经验 (2026-10-28): LLM「只展开、不下发」

问题句: 「九十九个香蕉平均分到三个仓库里去」——「三个仓库」没有「号」, 规则层抽不出
料场标记 → 空订单 PAUSE。用户问「能否让 LLM 来分发」。决策: **不让 LLM 直接下发**,
保持「任何 LLM 输出都过校验器」的安全不变量, 分两层:

1. **Tier-1 确定性展开** (毫秒级, 无 LLM, 路径 FAST): `平均/平分 + N 个物料 +
   M 个仓库/点名料场` 规则层自己除法, **余数给小号料场** (100÷3 → 34/33/33)。
   整除/余数都可算, ~200 ms 出结果, 快路径不掺 LLM。
2. **Tier-2 LLM 展开** (路径 EXPAND, ~10–16 s): 结构模糊 (未指明哪几个料场等) 时,
   LLM **只判断料场集合**, 数量由规则层按「余数给小号料场」确定性重算; 校验器
   **总量对账 (容差 0, 与规则层抽到的 N 精确一致)** + **物料必须与规则层在句子里听到的物料一致**
   (防幻觉, 不符逐项拒) → 通过再走 Laya 物料终审 → 下发; 对不上 → PAUSE+HUMAN, 提案给人工看。
3. **无数量的分配句** ("把香蕉平均分到仓库里") 不触发 LLM → 照旧 PAUSE 等人工。

一句话: **能算的不让 LLM 算, 算不了/说得模糊的才让 LLM 算, 且 LLM 永远过校验器。**
踩坑: prompt 里的示例 JSON 花括号会被 Python `.format()` 当占位符 → KeyError,
需写成 `{{"items":[...]}}` 双花括号 (v10 首次实测 500)。
**设计决策 (2026-10-29, QA LLM 兜底)**: 问答保持「规则层为主 (毫秒级/零幻觉)」,
新增**可选 LLM 兜底层** (默认关, `QA_LLM=1` 开启): 只对 规则层 miss 且 用户明确提问的
长尾问句触发; LLM **只能转述注入事实** (当前时间/服务端口实测/资源/料场白名单/最近
分拣指令), 事实外必须答「不知道」; 答案只展示 (QA 路径无 PLC 输出), 超时 20s/失败/
空答案 → 回落分拣路径 (PAUSE+HUMAN); 物料词一律视为分拣指令不进问答。
默认关的原因: 4b iGPU 实测 10~16s/请求 — 能确定性回答的绝不绕道 LLM。
**设计决策 (2026-10-29, PARTIAL)**: 超纲料场 (5号等) 混入有效订单 → **部分下发**:
在纲项按原话照发 (不重算), 超纲项挂起「超纲料场 N 号不下发 (…)», 路径 `PARTIAL`;
超纲号经 `ANY_YARD_RE` 参与分域归属 (防「5号放6个苹果」并入邻近 2号 域)。
但**分配指令**目标含超纲 → 整条暂停: 数量是除出来的, 目标少一个在纲项就分多,
规则层与 LLM 都不重算 (Tier-1/Tier-2 拒展开 + 哨兵提及 + 旁路幻觉防护三重兜底)。

### 出入库台账与库存问答 (2026-10-29): SQLite 台账 + 规则层直答

- 台账 `yard_ledger.py` (SQLite `data/yard_ledger.db`): FAST/EXPAND/PARTIAL 下发按每条
  action 自动记「入」, 页面「📦 仓库台账」面板手动记「出」(白名单+范围校验),
  库存 = 累计入 - 累计出 (料场×物料); 超纲挂起项不下发、不记账; 台账是统计旁路,
  记账失败不影响分发主路径。
- 库存问答 (都走 QA 规则层, 毫秒级, 不进 LLM; 数据源 = SQLite 台账 累计入-出):
  - 「1号料场有多少件」/「一号仓库里有多少货」/「3号仓库现在有多少货」
    = 该料场 累计库存 (默认答案, 含 物料明细 top2, 空仓答「暂无库存」);
  - 「现在各个仓库分别有多少内容」/「仓库库存」= 全部料场 累计库存 (门 _STOCK_RE);
  - 「最近一次/上次 1号料场的指令有多少」= 最近一次分拣指令的库存
    (loop_ui 注入, 显式 最近/上次/刚才 才进).
  语义演变 (2026-10-29 用户语音实测驱动): 「N号有多少货」最初默认答 最近一次指令,
  用户两次纠正后改为 默认答 台账 累计库存 — 仓库 = 存量, 不是「那一单」.
- LLM 兜底层的注入事实也带上「当前仓库库存」, 长尾问句 (如「哪个仓库内容最多」)
  可转述台账数字 (实测答对「3号最多, 43 件」)。
- 清空: `POST /ledger/reset` (DELETE+VACUUM, 服务运行中可调用) / 页面「🗑 清空台账」按钮;
  命令行等价指令见 README「仓库台账」节。

### PARTIAL 挂起通道扩展到「物料未定项」 (2026-10-29)

- 现象: 「一号库径三个方块, 2号库径4个苹果, 3号库径5个香蕉」(ASR 把料场识别成 库径,
  料场号解析不受影响) 整单 PAUSE — 校验器把 物料未定 (方块未说颜色) 当拒发理由,
  阻塞了两个 完全在纲 的项; 且 Laya 终审只对 已定物料 做 8 选 1 复核 (null 不送审,
  避免对「方块」无锚点瞎猜 — 实测基座会 conf=1 硬猜成红苹果)。
- 修复: `validate()` 中 material=None 的项路由到 oob 挂起列表 (与超纲料场同通道),
  附「N号 物料未定, 不下发 (xN, 需人工确认颜色/种类)」; 在纲项照发 → PARTIAL
  (实测 e2e 15.9s→169ms, 不再触发 LLM 旁路); 纯未定单因「空订单」仍 PAUSE, 行为不变。
- 原则: 能算的照发, 算不了/说不清的挂起报人工, 绝不让 LLM 猜颜色。

### LLM 在体系里的位置 (按设计)

| 角色 | 说明 | 状态 |
|---|---|---|
| ① 离线 teacher | 造"语音文本→意图"语料微调 Laya (laya-typed-decisions 路线) | ✅ 已实现 (`teacher_offline.py`, 见下) |
| ② 动作标签归纳 | 冻结 6~20 个正交动作 | 本测试冻结 4 个 |
| ③ 在线旁路 | 校验拒绝/超纲时兜底, 同校验器, 默认降级暂停 | ✅ 已实现 |
| ④ 开放问答/工单 | "3 号口今天卡了几次" 类, 不下发控制 | ✅ 部分接入 (简单问答: 规则层直答 + LLM 兜底默认关, 含台账库存) |

### ① 离线教师 (`teacher_offline.py`)

LLM (qwen3.5, Ollama) 只做离线侧, 不进实时路径:

1. **生成**: 规则模板 (5 名称族 × 8 句式 × 5 数量, 含同音字/繁体 ASR 噪声变体) +
   LLM 生成口语句。规则层做守护: 必须可解析、数量正确、无白名单外词, 不合格丢弃。
2. **交叉标注**: 每条句子再让 LLM 用要货解析 prompt (在线 BYPASS_PROMPT 的完整白名单版,
   允许 绿苹果) 独立解析一遍, 白名单校验 + 幻觉防护 (LLM 只能标规则层听到过的词)。
   与规则 gold 不一致 → 剔出训练集 + 记入 `teacher/qa_report.json`。
3. **微调**: 导出 `teacher/train.jsonl` / `teacher/eval.jsonl` (state 两种形态:
   回路原样 `用户要货: 梨 x 3` + 单意图句的语音转写原文, 保留同音字; 标签都是
   4 动作 choice, 与回路 `laya_decide` 同一个问题), `laya.train.finetune`
   微调 base `models/laya-multilingual` → `models/laya-fruits-teacher`。
4. **评测**: 留出集 base vs 微调; `--cases` 复现回路 4 个 CASES 的
   规则归一化 → Laya 终审 → 校验器, 对比快路径通过率。

```bash
.venv/bin/python teacher_offline.py generate   # 造语料 + 交叉标注 (约 10 min, 串行调用 Ollama)
.venv/bin/python teacher_offline.py train      # 微调 (CPU, 约 5 min)
.venv/bin/python teacher_offline.py eval --cases
.venv/bin/python teacher_offline.py all        # 一次全跑
```

实测 (2026-10-27, 本机 CPU, qwen3.5:4b @ Ollama):

- **thinking 不受控 (本机结论)**: qwen3.5:4b/2b 在 iGPU + Ollama 上开 `think=true`
  后思考段不终止 — 单句 2000 token 预算全部烧在 thinking、content 恒为空;
  `num_predict=8192` 同样 (thinking 14817 字符后截断, 仍无 JSON); 提示词限制思考长度
  无效, Ollama 的 `chat_template_kwargs.thinking_budget` 被忽略。标注/生成都是
  确定性短任务, 不依赖长链推理 → 离线教师**默认 thinking 关** (`--think` 可开,
  失败批自动降级重试, 供思考能正常终止的后端用); 在线旁路 `bench_industrial`
  同样 `think=false` (延迟优先)。
- **全流程实测 (qwen3.5:4b, CPU 训练, 约 1 h 总时长)**:
  - 生成: 250 规则模板 + LLM 新句 130 (单意图 116/120 + 多意图 14/40 通过规则层验证,
    30 条不合格丢弃), 交叉标注后再剔 37 (不一致 13 + 无法解析 24)
  - 交叉标注: 380 句全标, **一致率 90.3%** (agree 343 / mismatch 13 / unparseable 24);
    unparseable 全是 "离/李→梨" 同音字噪声句 (LLM 不认, 规则 gold 已知 → 剔出训练集,
    Laya 只学干净句, 同音字由在线规则层负责), mismatch 含 "来10个苹果"→LLM 标 1 等
  - 微调: 551 行 (496 训 + 55 校准), 4 epoch, 18.7 min, loss 0.024→0.0
  - 评测 (139 行留出集, 含 ASR 噪声 LLM 新句): **base 89.2% → 微调 100.0%**;
    弱项正是 base 的裸词漂移 red_apple 35/50 → 50/50, 其余动作两模型全对;
    在线回路 4 个 CASES: 两模型快路径 4/4、动作/数量全对; 单次 Laya ~115-122 ms
  - **上线**: `jev_api.py` 新增模型 `fruits-teacher` (别名 `jev-fruits`, 见 §4 ③),
    serve 路径 A/B: 4 CASES 逐提及 base 8/8 / teacher 8/8 (规范句两模型都对, 教师优势
    在未见 ASR 噪声句上); 默认 `jev-latest` 不变, 在线回路零改动, 需要时把
    `bench_industrial.py` 的 model 换成 `jev-fruits` 即可
  - **在线回路回归** (bench_industrial.py 3 轮, 含 .isdigit() 守护改动): 12/12 快路径,
    12/12 正确, E2E mean 334 ms / p95 488 ms

---

## 5. 踩坑记录 (排查过的问题)

| 现象 | 根因 | 修复 |
|---|---|---|
| `import bench_industrial` 崩溃 (`invalid literal for int()`) | 模块顶层 `int(sys.argv[1])` 解析调用方 argv | `REPS` 行加 `.isdigit()` 守护 (同 bench_pipeline), `teacher_offline.py` 才能 import 复用 `normalize/validate/NAME2ACTION` |
| LLM 请求 10+ 分钟无响应, response 空 | qwen3.5 是思考模型, 200 token 被 thinking 吃光 | 请求体加 `"think": false` |
| thinking 模式 `num_predict` 2000/8192 都吃光、content 恒空 | iGPU + Ollama 上 qwen3.5 思考**不受控**: 2b/4b 同样, 提示词限长无效, Ollama 不支持 `thinking_budget` | 确定性解析任务 (离线教师标注/在线旁路) 一律 `think=false`; `--think` 仅在思考能正常终止的后端 (CUDA / 支持 thinking_budget) 上开, 失败批自动降级 |
| `jev_api.py` 加了新模型却 422 `unknown model` | 白名单校验硬编码 `("english","multilingual")`, 新 `MODELS` 键进不了 | 改成 `force not in MODELS` 通用校验 (加模型只动 `MODELS` + `MODEL_ALIASES` 两处) |
| ollama CPU 0.2%、请求全挂起 | 3 个重复 `run_all.sh` 并发互抢 | pkill + 锁文件 |
| registry.ollama.ai network unreachable | 海外站点时断时续 | 直接重试 (断点续传 20s 收尾) |
| HF / hf-mirror 不通 | 网络封锁 | whisper 走 modelscope (`pengzhendong/faster-whisper-small`), TTS 走 edge-tts |
| `av 19` 与 faster-whisper 不兼容 | `open(metadata_errors=)` 被移除 | `uv pip install av==12.3.0` |
| espeak 中文机器音 | ASR 匹配率 7~59% | edge-tts 自然语音 |
| 多问题单请求 Laya 漂判 | 问题间共享 state 无法区分提及 | 每提及单独请求, state 含物品名 |
| 纯否定句 LLM 编整单 | 幻觉 | 幻觉防护 (LLM 项 ⊆ 规则层听到词) |

### 调试批次 (2026-10-28, 料场域 + 前端 v4→v10)

| 现象 | 根因 | 修复 (commit) |
|---|---|---|
| 预设按钮点击无反应 `Unexpected end of input @1` | 内联 onclick 里 `JSON.stringify` 的**嵌套双引号把 HTML 属性截断**, 点击时 V8 编译残缺 handler 抛错; 叠加服务重启瞬间旧标签页收到半截 HTML | 预设数据改 JS 数组 `PREDS`, onclick 只传数字索引 (属性里绝不嵌套 JSON/引号) `cfe84f7`; 页面带版本号 (v3→v10) 区分新旧标签页 |
| 「按空格影响的是授权按钮」 | 焦点停在按钮上时, 空格 keyup 触发**按钮激活** → `authToggle()` 二次触发撤销授权 | BUTTON 焦点时空格 keydown/keyup 均 `preventDefault`, PTT 照常 (授权后直接空格录音) `a88fdeb` |
| `Cannot read properties of undefined (reading 'preventDefault')` | 空格 handler 调 `pttDown()` **没传事件对象**, 函数首行 `e.preventDefault()` 抛错 | `if(e)e.preventDefault();` `7bb7abb`; DOM 桩 `test_ptt.js` 先在 v6 复现同一错误再修 |
| 「二号」指令全被拒、「七号」KeyError | `YARD_RE` 字符类 `[123一三七幺壹]` **漏「二」多「七」** (笔误) | `[123一二三幺壹]` + T2S 补 `號→号` `8a8bdec` |
| 无标点密集句误归属 («两个苹果» 归了二号) | 子句切分对**无标点句失效** → 退回全局最近距离, 字面紧贴二号的苹果被误归 | 弃用子句切分/最近距离, 改**正序分域** (料场标记拥有 [自身起点, 下一标记起点) 的物料) + **倒序黏附** (物料尾与下一标记 gap≤4 且含运送动词 → 归下一标记) `db561db` |
| «两号» 不认 → 2号域丢失, 物料并入 1号 | 口语 «两号/俩号» 不在正则 (只有 «二») | `YARD_RE`/`YARD_NUM` 补 两/俩, 最终字符类 `[123一俩二两三幺壹]` `5b8cbe2` |
| 混合订单混进 5号料场 → 整单暂停 (用户要求 1号2号照发) | 原设计「超纲混入即整单暂停」: 在纲项数量明明已说死, 被超纲项连坐 | **部分下发 PARTIAL**: 超纲号经 `ANY_YARD_RE` 参与分域归属 (防并入邻近在纲域), 校验器把超纲项路由到 oob 挂起 (不进拒发 reasons), 在纲项照发; 分配句含超纲目标仍整条暂停 (数量是除出来的, 不许重算) 基准 25/25 |
| 分配展开首次请求 500 | prompt 示例 JSON 花括号被 `.format()` 当占位符 → KeyError | 双花括号 `{{...}}` `04f0864` (见上「分配指令经验」) |
| 分配余数给大号 (3/3/4 而非 4/3/3) | 4b 模型不遵守 prompt 里的「余数给小号」规则, 分配随机漂移 | 数量改由**规则层对 LLM 的料场集合确定性重算** (LLM 只判料场, qty 降为参考值) `b2ebfd3` |

**验证链 (现为标配)**: `py_compile` → 重启服务 → curl 线上 → 从交付 HTML 提取
`<script>` 跑 `node --check` → headless Chromium `--dump-dom` 实测渲染 → DOM 桩测试
(`test_ptt.js` 空格链路 / `test_render.js` 卡片渲染) → `/run` 回归锚点 → commit + push
+ 开新标签页。

**运维教训**:
- `pkill -f "loop_ui.py"` 会**自匹配**——自己的 shell 命令行里也含这个字面串, 把自己杀掉;
  用字符类技巧 `pkill -f "loop_ui[.]py"`, 且 kill 与重启必须分两条命令 (重启段含字面名)。
- 校验线上下发的 JS 时, 提取脚本正则写错会静默产出 0 字符的假 OK——务必打印提取长度。
- `git add -A` 会捎走遗留未跟踪文件 (04f0864 把 bench_kit.py/sort_mqtt.py 捎进提交),
  提交用显式文件清单。

---

## 6. 建议 (下一步)

1. ~~**离线 teacher 语料**~~ ✅ 已实现 (`teacher_offline.py`, 见 §4 ①): qwen3.5
   生成 + 交叉标注 → Laya 微调, 治"裸词漂移"的根
2. **动作集配置化**: `FRUIT_CRIT` / 规则表 / 同音字表抽成 YAML, 扩 6~20 动作时
   只改配置 + 重跑 `bench_industrial.py` 回归
3. **多意图并行化**: 每提及一次 Laya 请求目前串行 (241ms), 并行可再压 ~一半
4. **GPU**: CPU 上 ASR 1.3s 是大头; 入门 GPU 后语音 E2E 可进 500ms 内
5. **真实语音验证**: 当前用 TTS 合成, 建议补真人录音 (含噪声/口音) 回归
