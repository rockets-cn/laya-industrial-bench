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

### LLM 在体系里的位置 (按设计)

| 角色 | 说明 | 状态 |
|---|---|---|
| ① 离线 teacher | 造"语音文本→意图"语料微调 Laya (laya-typed-decisions 路线) | ✅ 已实现 (`teacher_offline.py`, 见下) |
| ② 动作标签归纳 | 冻结 6~20 个正交动作 | 本测试冻结 4 个 |
| ③ 在线旁路 | 校验拒绝/超纲时兜底, 同校验器, 默认降级暂停 | ✅ 已实现 |
| ④ 开放问答/工单 | "3 号口今天卡了几次" 类, 不下发控制 | 未接入 |

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

---

## 6. 建议 (下一步)

1. ~~**离线 teacher 语料**~~ ✅ 已实现 (`teacher_offline.py`, 见 §4 ①): qwen3.5
   生成 + 交叉标注 → Laya 微调, 治"裸词漂移"的根
2. **动作集配置化**: `FRUIT_CRIT` / 规则表 / 同音字表抽成 YAML, 扩 6~20 动作时
   只改配置 + 重跑 `bench_industrial.py` 回归
3. **多意图并行化**: 每提及一次 Laya 请求目前串行 (241ms), 并行可再压 ~一半
4. **GPU**: CPU 上 ASR 1.3s 是大头; 入门 GPU 后语音 E2E 可进 500ms 内
5. **真实语音验证**: 当前用 TTS 合成, 建议补真人录音 (含噪声/口音) 回归
