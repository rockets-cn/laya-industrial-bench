# Laya 工业决策回路（laya-industrial-bench）

在 [Laya](https://github.com/NandhaKishorM/laya)（非自回归 System 1 决策模型）之上构建并压测一条
**语音/文本工业决策回路**。本仓库 = 上游 Laya 引擎 + 本项目新增的回路、基准、教师与部署（下表已标注）。

## 架构

```
语音/文本 -> ASR(SenseVoice int8) -> 规则归一化 -> Laya 结构化决策(:8000)
          -> 确定性校验器 -> (LLM 旁路: 仅校验拒绝时) -> PLC 下发
```

三条设计原则（完整论证见 [`VOICE_DECISION_BENCH.md`](VOICE_DECISION_BENCH.md)）：

1. **实时路径不含 LLM**。规则层做确定性抽取（颜色/数量/否定，ms 级），Laya 做单次前向的结构化
   决策（~120 ms/提及，白名单选项），校验器做白名单 + 数量范围 + 交叉检查。
2. **LLM（qwen3.5:4b，Ollama :11434）仅旁路**。只有校验器拒绝时才调用；旁路输出必须过同一个
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

领域可插拔：每条回路都是 `normalize()`（词表/正则）+ `laya_decide()` + `validate()` 三件套，
换词表即新领域。视觉侧另有 `bench_colorblock.py`：Laya 看不到像素，颜色块识别由像素层
HSV 阈值完成（mean 3.6 ms / p95 4.6 ms），Laya 只在下游做决策与交叉校验。

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
| `loop_ui.py` | Web 控制台（`:8002`）：领域切换（水果/方块/混合/料场分拣）、预设、麦克风授权+按住说话/录音识别（唤醒词可选开关）、全链路 trace；纯转发不载模型 |
| `teacher_offline.py` | 水果域离线教师：qwen4b 造语料 + 交叉标注 + Laya 微调 |
| `teacher_blocks.py` | 方块域离线教师：生成 + 交叉标注 + 规则层审计 → `teacher/blocks_corpus.jsonl` |
| `teacher/` | 语料与训练/评测数据（jsonl） |
| `models/` | checkpoint 与微调模型（`laya-fruits-teacher` 等，gitignored） |
| `test_audio/` | 基准用 TTS 音频与录音 |
| `deploy/` | 开机自启：systemd 用户服务 ×3 + 浏览器 autostart |
| `scripts/open_loop_ui.sh` | 开机时等服务就绪后开浏览器 |
| `VOICE_DECISION_BENCH.md` / `TEACHER_REPORT.md` / `DEPLOY_SENSEVOICE.md` | 链路设计报告 / 教师报告 / 部署记录 |

**上游 Laya 引擎**（原样保留，文档见 [`LOCAL_SETUP.md`](LOCAL_SETUP.md)）：
`laya/`（Python 包）、`sdk/`、`docs/`、`research/`、`benchmarks/`、`notebooks/`、
`laya-dotnet/` `laya-java/` `laya-ts/`、`tests/`、`verify/`、`docker/`。

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
