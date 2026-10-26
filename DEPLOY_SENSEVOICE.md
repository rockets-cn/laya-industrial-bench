# SenseVoice + Laya 工业决策回路 — 部署需求表

> 目标: 在一台新的 Omarchy (Arch) 设备上重新部署语音下单工业决策回路,
> ASR 由 faster-whisper 换为 **SenseVoice (voxtype CLI)**, 其余链路与
> `bench_industrial.py` 保持一致。
>
> 参考来源: 本机 (15 GB RAM, CPU only) 2026-10-08~09 的实测, 见 `VOICE_DECISION_BENCH.md`。

---

## 1. 架构 (部署目标)

```
语音 ──► ASR ──► 规则归一化 ──► Laya 结构化决策 ──► 确定性校验器 ──► PLC 下发
        voxtype    简繁/同音字/    (4 动作白名单,     (白名单+交叉      (干跑)
        (SenseVoice) 否定剥离/      每提及单独请求      检查, 越界拒绝)
                    数量抽取          via jev_api:8000
                                          │ 拒绝
                                          ▼
                                   LLM 旁路 (Ollama qwen3.5:2b, think=false)
                                          │ 过同一个校验器 + 幻觉防护
                                          ▼ 仍失败
                                   PAUSE + 人工确认
```

实时路径不含 LLM; LLM 仅旁路兜底; 任何失败安全降级为暂停+人工。

---

## 2. 硬件 / 系统环境

| 项 | 要求 | 说明 |
|---|---|---|
| OS | Omarchy (Arch, Hyprland) | x86_64 |
| CPU | x86_64, ≥4 核 | 本机 CPU only 可跑 |
| RAM | ≥8 GB (建议 15 GB) | 本机 15 GB; 三模型常驻: Laya ~2.5 GB + qwen3.5:2b ~2.7 GB + ASR/系统 |
| GPU | 不需要 | 全部 CPU (torch+cpu, int8 ASR) |
| 磁盘 | ≥15 GB 空闲 | 模型资产 ~6 GB + venv(torch) ~5 GB + 余量 |
| 网络 | 能访问: modelscope.cn / ollama 镜像 / GitHub; HF 可能不通 | 模型下载走 ModelScope 与 hf-mirror, 见 §5 |

---

## 3. 系统层依赖 (pacman / AUR)

```bash
omarchy pkg add git python uv ffmpeg sox      # ffmpeg/sox: edge-tts mp3 -> 16k mono wav
omarchy pkg aur add voxtype-bin               # pre-built voxtype 1.1.0 (提供 SenseVoice ASR)
```

| 包 | 用途 | 备注 |
|---|---|---|
| `voxtype-bin` (AUR) | ASR CLI: `voxtype -q transcribe --engine sensevoice <wav>` | 无需 PyTorch; 输入要求 **16 kHz mono WAV** |
| `ffmpeg` / `sox` | 测试音频转 16k mono wav | edge-tts 产出 24k mp3, 必须转码 |
| `python` + `uv` | Python 3.10~3.13 (本机 3.12) 与 venv 管理 | 可用 mise 管理 |
| `ollama` | LLM 旁路 qwen3.5:2b | 本机已装, 新设备 `omarchy pkg add ollama` 或 curl 脚本 |

验证:
```bash
voxtype --help | grep sensevoice    # 引擎可用
voxtype setup                         # 首次拉取 sensevoice-small 模型 (229 MB)
ffmpeg -i x.wav                       # 确认 16000 Hz, mono
```

---

## 4. Python 层依赖 (项目 venv)

```bash
cd ~/Work/laya
uv venv .venv -p 3.12
uv pip install --python .venv/bin/python \
    "torch>=2.0" --index-url https://download.pytorch.org/whl/cpu \
    "transformers>=4.48" safetensors huggingface_hub "numpy<2.3" \
    fastapi uvicorn edge-tts modelscope
uv pip install --python .venv/bin/python -e .          # laya 本体 (editable)
```

| 依赖 | 版本参考 (本机) | 用途 |
|---|---|---|
| torch | 2.14.1+**cpu** | Laya 推理 (务必 CPU 版, 免 CUDA 依赖) |
| transformers / safetensors / huggingface_hub | 5.19 / 0.8 / 1.33 | laya 依赖 |
| numpy | <2.3 (本机 2.5.3 亦可, 注意 ONNX 路径才需压 1.x) | — |
| fastapi + uvicorn | 0.142 / 0.54 | `jev_api.py` HTTP API (:8000) |
| edge-tts | 7.2 | 生成测试语音 (zh-CN-XiaoxiaoNeural); **espeak 机器音不可用** (ASR 匹配率 7~59%) |
| modelscope | 1.40 | SenseVoice 备用下载渠道 (`iic/SenseVoiceSmall`) |
| (可选) faster-whisper + ctranslate2 + av==12.3.0 | 1.2.1 / 4.8.2 / 12.3.0 | 保留 whisper 作为对照基线; 不装则 bench_industrial.py 语音模式改用 voxtype 分支 |

---

## 5. 模型资产清单

| 模型 | 来源 | 大小 | 落位 | 获取方式 |
|---|---|---|---|---|
| Laya 三检查点 (english / multilingual / typed-decisions) | `convaiinnovations/laya` (HF) | ~2.3 GB | `~/Work/laya/models/laya/{english,multilingual,typed-decisions}` | HF 不通时 `HF_ENDPOINT=https://hf-mirror.com`; 或直接 rsync 本机 `~/Work/laya/models/laya` |
| SenseVoiceSmall | voxtype 自动 / `iic/SenseVoiceSmall` (ModelScope) | 229 MB (fp32 895 MB) | `~/.local/share/voxtype/models/sensevoice-small` | `voxtype setup` 或首跑自动下载; 也可 rsync 本机目录 |
| qwen3.5:2b (LLM 旁路) | Ollama | ~2.7 GB | `~/.ollama/models` | `ollama pull qwen3.5:2b` (海外 registry 不稳时重试, 支持断点续传) |

布局与本机保持一致 (符号链接勿丢):
```
models/laya/                      # 实体
models/laya-multilingual -> laya/multilingual
models/laya-typed-decisions -> laya/typed-decisions
```

---

## 6. 代码 / 目录结构

```
~/Work/laya/
├── laya/                 # Python 包 (上游 clone, commit 3cf26cb)
├── jev_api.py            # Jev 线格式 HTTP API, :8000 (POST /v1/systemone)
├── bench_industrial.py   # 工业回路主程序 (需按 §7 改 ASR 分支)
├── bench_pipeline.py     # llm_json() 等工具函数 (bench_industrial 依赖它)
├── bench_fruit.py / bench_split.py   # 基线对照 (可选)
├── run_all.sh            # 带锁的自动等待+运行 (锁文件 /tmp/run_all.lock)
├── models/               # §5
└── test_audio/           # tts_1..4.wav (16k mono, 由 edge-tts 生成)
```

测试音频在新设备重新生成 (edge-tts 音色 `zh-CN-XiaoxiaoNeural`):
```bash
.venv/bin/python - <<'EOF'
import asyncio, edge_tts, subprocess, pathlib
TEXTS = ["我要那个苹果，我要那个梨，给我十个香蕉",
         "来两个红苹果和一把香蕉",
         "只给我三个梨就行",
         "一个苹果，不要梨，五个香蕉谢谢"]   # 与 CASES 对齐
async def main():
    for i, t in enumerate(TEXTS, 1):
        mp3 = f"test_audio/tts_{i}.mp3"
        await edge_tts.Communicate(t, "zh-CN-XiaoxiaoNeural").save(mp3)
        # voxtype 要求 16k mono wav
        subprocess.run(["ffmpeg", "-y", "-i", mp3,
                        "-ar", "16000", "-ac", "1", f"test_audio/tts_{i}.wav"], check=True)
asyncio.run(main())
EOF
```

---

## 7. 代码改造点 (whisper → SenseVoice)

`bench_industrial.py` 中 `asr()` 替换为 voxtype CLI (参考 calling2Socrates/chat.py 的调用方式):

```python
import subprocess
def asr(wav):
    t0 = time.perf_counter()
    r = subprocess.run(["voxtype", "-q", "transcribe",
                         "--engine", "sensevoice", wav],
                        capture_output=True, text=True, timeout=120)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    return time.perf_counter() - t0, lines[-1].strip()   # 最后一行是转写结果
```

适配注意:

1. **同音字表**: SenseVoice 与 whisper 的同音字错误分布不同 (whisper: 梨→離/离),
   部署后先跑 4 句测试音频, 把实际错误字补进 `HOMOPHONE` 表再回归。
2. **中文数字/量词**: SenseVoice 输出可能直接给阿拉伯数字 ("10个"),
   `_qty_before()` 已兼容 `\d`; 但可能省略"个"字, 部署后抽查。
3. 其余 (normalize / laya_decide / validate / llm_bypass / CASES) **不动**,
   以保证与本机 40/40 结果可比。

---

## 8. 服务清单

| 服务 | 端口 | 启动 | 健康检查 |
|---|---|---|---|
| jev_api (Laya HTTP) | 8000 | `.venv/bin/python jev_api.py` | `curl -s 127.0.0.1:8000/health` 或一次最小 predict |
| Ollama (旁路 LLM) | 11434 | systemd 服务 `ollama` | `ollama list \| grep qwen3.5:2b` |
| voxtype | — (无守护进程, 按需 CLI) | — | `voxtype transcribe --engine sensevoice test_audio/tts_1.wav` |

---

## 9. 验收标准 (回归基线 = 本机 whisper 版实测)

| 测试 | 命令 | 通过 |
|---|---|---|
| 文本回路 5 轮 × 4 句 | `.venv/bin/python bench_industrial.py 5 text` | 正确率 100% (20/20), 路径全 FAST, E2E mean < 300 ms |
| 语音回路 5 轮 × 4 句 | `.venv/bin/python bench_industrial.py 5 voice` | 正确率 100% (20/20), E2E mean < 2.0 s |
| 超纲降级 ① | 输入 "把三号口的窗户关上" | 落 PAUSE+HUMAN |
| 超纲降级 ② | 输入 "不要香蕉" | 落 PAUSE+HUMAN (幻觉项被拦截) |
| 单意图快路径 | 输入 "给我来十个苹果" | FAST, 数量=10 |

语音验收参考值 (SenseVoice CPU 预计): E2E mean 1.3~2.0 s, 其中 ASR < 1 s (SenseVoice 比 whisper-small 快), Laya 段 < 500 ms。
若语音正确率 < 100%, 先查 §7 同音字表, 再决定是否放宽。

---

## 10. 踩坑清单 (本机已验证, 部署时直接规避)

| 坑 | 规避 |
|---|---|
| ollama 思考模型空响应/超时 | 所有 LLM 请求体带 `"think": false` |
| 多个 bench 并发把 ollama 拖死 (CPU 0.2% 假死) | `run_all.sh` 锁文件; 部署回归一次只跑一个 |
| HF / hf-mirror 不通 | Laya 检查点: hf-mirror 或 rsync 本机; ASR: modelscope/voxtype |
| voxtype 输入格式 | 必须 16 kHz mono WAV; edge-tts 的 mp3 先 ffmpeg 转码 |
| espeak 机器音 | 测试语音一律 edge-tts 自然音 |
| Laya 多问题单请求裸词漂移 | 保持"每提及单独请求 + state 写成 `用户要货: X x N`" 的问法, 勿改 |
| 计时偏差 | 若仍保留 faster-whisper 分支: `transcribe()` 是惰性生成器, 消费完再计时 |
| 置信度虚高 | 校验器是硬约束, 永远不要退化成"置信度阈值兜底" |

---

## 11. 部署步骤 (顺序)

1. `omarchy pkg add git ffmpeg sox python uv ollama` + `omarchy pkg aur add voxtype-bin`
2. `git clone https://github.com/NandhaKishorM/laya ~/Work/laya` (checkout `3cf26cb`) 或 rsync 本机仓库 (含本地实验脚本)
3. 建 venv + 装依赖 (§4)
4. `voxtype setup` 拉 SenseVoiceSmall; `ollama pull qwen3.5:2b`
5. 拉 Laya 检查点到 `models/laya/` 并建符号链接 (§5)
6. 生成 `test_audio/tts_1..4.wav` (§6)
7. 改 `bench_industrial.py` ASR 分支 (§7), 补 SenseVoice 同音字表
8. 起 jev_api, 健康检查
9. 跑文本回归 (20/20) → 跑语音回归 (20/20) → 3 条超纲用例 (§9)
10. 全绿后记录 E2E 分段数据, 更新本报告 §9 实测列
