# 离线教师 (Offline Teacher) — 总结报告

日期: 2026-10-28 · 机器: Linux x86_64, iGPU (Ollama ~25 tok/s) + CPU 训练 · 教师模型: qwen3.5:4b @ Ollama

## 目标

实现 `VOICE_DECISION_BENCH.md` 设计 ①: 用 LLM 离线生成"语音文本→意图"语料,
交叉标注后微调 Laya (`models/laya-multilingual` → `models/laya-fruits-teacher`),
治"裸词 苹果 漂移"的根。LLM 不进实时路径 (设计 §4 核心原则)。

## 交付物

| 文件 | 说明 |
|---|---|
| `teacher_offline.py` | 离线教师全流程: `generate` / `train` / `eval [--cases]` / `all` |
| `teacher/corpus_full.jsonl` | 380 句语料 (250 规则模板 + 130 LLM 生成, 含同音字/繁体 ASR 噪声) |
| `teacher/train.jsonl` / `teacher/eval.jsonl` | 551 / 139 行 Laya 微调数据 (state 两种形态: 回路原样 + 语音转写原文) |
| `teacher/qa_report.json` | 交叉标注 QA: 一致率、mismatch/unparseable 明细、丢弃样本 |
| `teacher/eval_report.json` | 留出集 base vs 微调 评测 (acc / 延迟 / 分动作) |
| `models/laya-fruits-teacher/` | 微调 checkpoint (gitignored, 643 MB) |
| `jev_api.py` | serve 新增模型 `fruits-teacher` (别名 `jev-fruits`); 修掉硬编码模型白名单 |
| `bench_industrial.py` | `REPS` 行加 `.isdigit()` 守护 (模块顶层 `int(sys.argv[1])` 曾致 import 崩溃) |

## 关键决策: thinking 实测结论

qwen3.5 是思考模型, 原计划教师环节开 thinking (质量优先)。实测本机 iGPU + Ollama:

- 单句标注 2000 token 预算全部烧在 thinking 段, **content 恒为空**;
- `num_predict=8192` 同样: thinking 14817 字符后截断, 仍无 JSON; 2b 同样;
- 提示词限制思考长度无效; Ollama 的 `chat_template_kwargs.thinking_budget` 被忽略。

结论: 本机离线教师 **默认 thinking 关** (标签是确定性短任务, 不依赖长链推理,
实测 1.2 s/句)。`--think` 开关保留: 生成/新句标注切到 thinking 批量 (4 句/批
@4096, 失败批自动降级重试), 供思考能正常终止的后端使用。在线旁路
`bench_industrial` 维持 `think=false` (延迟优先)。

## 全流程实测

| 阶段 | 结果 |
|---|---|
| 生成 | 250 规则模板 + LLM 新句 130 (单意图 116/120 + 多意图 14/40 通过规则层验证, 30 条丢弃) |
| 交叉标注 | 380 句全标 (qwen3.5:4b, 无 thinking, 句均 1248 ms), **一致率 90.3%** (agree 343 / mismatch 13 / unparseable 24) |
| 剔除 | 37 句剔出训练集: unparseable 全是 "离/李→梨" 同音字噪声 (LLM 不认, 规则 gold 已知 → Laya 只学干净句, 同音字由在线规则层负责); mismatch 含 "来10个苹果"→LLM 标 1 |
| 微调 | 551 行 (496 训 + 55 校准), 4 epoch, CPU 18.7 min, loss 0.024 → 0.0 |
| 评测 | 139 行留出集 (含 ASR 噪声 LLM 新句): **base 89.2% → 微调 100.0%**; 弱项正是 base 的裸词漂移 red_apple 35/50 → 50/50, 其余动作两模型全对; 单次 Laya ~115-122 ms |
| 在线回路 4 CASES | base / 微调: 快路径 4/4, 动作数量全对 |
| 在线回路回归 | `bench_industrial.py 3` (语音全流程): **12/12 快路径, 100% 正确**, E2E mean 334 ms / p95 488 ms |
| serve 路径 A/B | 4 CASES 逐提及 (真实 FRUIT_CRIT): base 8/8 / teacher 8/8 — 规范句两模型都对, 教师优势在未见 ASR 噪声句上 |

分动作 (留出集, base → 微调):

| 动作 | base | 微调 |
|---|---|---|
| red_apple | 35/50 | **50/50** |
| green_apple | 32/32 | 32/32 |
| banana | 30/30 | 30/30 |
| pear | 27/27 | 27/27 |

## 用法

```bash
.venv/bin/python teacher_offline.py generate          # 造语料 + 交叉标注 (~10 min)
.venv/bin/python teacher_offline.py generate --think  # 同上, 开 thinking (实验性, 失败批自动降级)
.venv/bin/python teacher_offline.py train             # 微调 (CPU ~19 min)
.venv/bin/python teacher_offline.py eval --cases      # 留出集 + 在线回路 4 CASES
.venv/bin/python teacher_offline.py all               # 一次全跑
```

上线 (可选, 默认不变): `jev_api.py` 已注册教师模型, serve 上请求 `model: "jev-fruits"`
即用微调模型; 在线回路默认仍走 `jev-latest` (base)。需要时把 `bench_industrial.py`
的 `laya_decide` 中 model 换成 `jev-fruits` 即可。

## 踩坑 (详见 VOICE_DECISION_BENCH.md §5)

- qwen3.5 thinking 在本机不受控 (8192 预算仍不出 JSON) → 离线教师默认 think=false
- `bench_industrial.py` 模块顶层 `int(sys.argv[1])` 致 import 崩溃 → `.isdigit()` 守护
- `jev_api.py` 模型白名单硬编码 `("english","multilingual")`, 加新模型必 422 → 通用 `force not in MODELS` 校验
