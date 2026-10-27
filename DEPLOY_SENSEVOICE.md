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
                                   LLM 旁路 (Ollama qwen3.5:4b, think=false, 全层 iGPU)
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
| RAM | ≥8 GB (建议 15 GB) | 本机 15 GB; 三模型常驻: Laya ~2.5 GB + qwen3.5:4b ~3.1 GB + ASR/系统 |
| GPU | 可选 (推荐 Intel Arc iGPU) | 4b 可整模跑 iGPU (Vulkan); 无 GPU 时 CPU 也能跑 (更慢) |
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
| `ollama` | LLM 旁路 qwen3.5:4b | 本机已装, 新设备 `omarchy pkg add ollama` 或 curl 脚本 |

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
| sherpa-onnx | 2.0.1 | **ASR 常驻引擎**: 进程内加载同一个 SenseVoice ONNX (免 CLI 冷启动 ~750ms); `bench_industrial.asr()` 首选, 失败自动回退 voxtype CLI |
| (可选) faster-whisper + ctranslate2 + av==12.3.0 | 1.2.1 / 4.8.2 / 12.3.0 | 保留 whisper 作为对照基线; 不装则 bench_industrial.py 语音模式改用 voxtype 分支 |

---

## 5. 模型资产清单

| 模型 | 来源 | 大小 | 落位 | 获取方式 |
|---|---|---|---|---|
| Laya 三检查点 (english / multilingual / typed-decisions) | `convaiinnovations/laya` (HF) | ~2.3 GB | `~/Work/laya/models/laya/{english,multilingual,typed-decisions}` | HF 不通时 `HF_ENDPOINT=https://hf-mirror.com`; 或直接 rsync 本机 `~/Work/laya/models/laya` |
| SenseVoiceSmall | voxtype 自动 / `iic/SenseVoiceSmall` (ModelScope) | 229 MB (fp32 895 MB) | `~/.local/share/voxtype/models/sensevoice-small` | `voxtype setup` 或首跑自动下载; 也可 rsync 本机目录 |
| qwen3.5:4b (LLM 旁路) | Ollama | ~3.3 GB | `~/.ollama/models` | `ollama pull qwen3.5:4b` (海外 registry 不稳时重试, 支持断点续传) |

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

`bench_industrial.py` 中 `asr()` 改为**进程内常驻引擎** (sherpa-onnx, 2026-10-27):

```python
def _resident_recognizer():
    """模型只加载一次 (~0.7s), 之后每句 ~60ms. voxtype CLI 仅作回退."""
    import sherpa_onnx
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        tokens=f"{mdir}/tokens.txt", model=f"{mdir}/model.int8.onnx",
        num_threads=4, language="zh", use_itn=False)

def asr(wav):
    st = _resident_recognizer().create_stream()
    st.accept_waveform(16000, <float32 samples>); rec.decode_stream(st)
    return dt, _fix_negation_punct(st.result.text)
```

要点: 用**同一份** voxtype 已下载的 SenseVoice ONNX (`~/.local/share/voxtype/models/sensevoice-small/`),
不另拉模型; CLI 分支 (`_asr_cli`) 保留, 常驻引擎异常时自动回退。

适配注意:

1. **同音字表**: SenseVoice 与 whisper 的同音字错误分布不同 (whisper: 梨→離/离),
   部署后先跑 4 句测试音频, 把实际错误字补进 `HOMOPHONE` 表再回归。
   2026-10-27 部署实测: SenseVoice 对 4 句测试音频无同音字错误, `HOMOPHONE` 未改动;
   但 SenseVoice 会丢否定词前的逗号 ("一个苹果不要梨，五个香蕉，谢谢。"), 使规则层
   的 `苹果(不要)` 分支误判——已在 `asr()` 内加 `_fix_negation_punct()`: 水果名后紧跟
   否定词且否定词后还有汉字/数字宾语时补一个逗号 (句尾 "梨不要" 不触发, 规则层不动)。
2. **中文数字/量词**: 常驻引擎 `use_itn=False`, 输出中文数字 ("十个"),
   `_qty_before()` 本就解析中文数字; sherpa-onnx 的 ITN 有 bug ("十"→"1") 故不用。
   注意输出**无标点**, 否定正则已改为跨水果名限长 (§10)。
3. 其余 (normalize / laya_decide / validate / llm_bypass / CASES) **不动**,
   以保证与本机 40/40 结果可比。

---

## 8. 服务清单

| 服务 | 端口 | 启动 | 健康检查 |
|---|---|---|---|
| jev_api (Laya HTTP) | 8000 | `.venv/bin/python jev_api.py` | `curl -s 127.0.0.1:8000/health` 或一次最小 predict |
| Ollama (旁路 LLM, **Vulkan iGPU**) | 11434 | `env OLLAMA_IGPU_ENABLE=1 GGML_BACKEND_PATH=$HOME/.local/share/ollama-gpu/vulkan/libggml-vulkan.so ollama serve` | `ollama ps` 看 PROCESSOR = `100% GPU` |
| voxtype | — (无守护进程, 按需 CLI) | — | `voxtype transcribe --engine sensevoice test_audio/tts_1.wav` |
| loop_ui (Web 测试台, 含唤醒词) | 8002 | `.venv/bin/python loop_ui.py` | `curl -s -o /dev/null -w %{http_code} 127.0.0.1:8002/` |

**唤醒词机制 (loop_ui)**: 点🎤进入 idle 态, 滚动 3s 音频窗口在说话间歇 (~0.6s 静音) 送 `/run {wake:true}` 只做 ASR;
ASR 文本命中 `优优|悠悠|由由|柚柚|youyou` → 进入 cmd 态听指令 (1.2s 静音识别, 30s 无话超时回 idle)。
一口气说"优优给我两个苹果"时, 去掉唤醒词后的剩余文本直接跑管线, 不用重说。
⚡手动识别 按钮可绕过唤醒词直接识别最近 ~5s。"优优" 经 SenseVoice 常识别为 "悠悠" (同音字), 正则已覆盖。

**Ollama Vulkan iGPU (2026-10-27 已启用)**: 本机 Intel Core Ultra 200V Arc iGPU (xe 驱动)。
Arch 的 `ollama` 包不含 `libggml-vulkan.so` (在分离包 `ollama-vulkan` 里, 装它需 root), 用户级做法:
1. 从 mirror 下 `ollama-vulkan-<ver>-1-x86_64.pkg.tar.zst`, 提取 `usr/lib/ollama/vulkan/libggml-vulkan.so` → `~/.local/share/ollama-gpu/vulkan/`
2. 启动时带两个变量: `OLLAMA_IGPU_ENABLE=1` (默认丢弃集显!) + `GGML_BACKEND_PATH=<指向 .so 文件, 不是目录>`
3. 验证: `ollama ps` → `100% GPU`; LLM 推理走 iGPU, CPU 留给 ASR/Laya。
   注意: 4b 自动 offload 只给 ~42% GPU, 必须请求体带 `options.num_gpu=99` 才整模上 iGPU (bench/UI 已内置)。
其他模型上 GPU: **ASR (sherpa-onnx ONNX) 的官方 Linux wheel 不支持 Vulkan EP** (仅 CPU), 但推理已 ~60ms 无收益;
**Laya (torch CPU wheel)** 上 GPU 需 Intel XPU 版 torch (oneAPI), 部署成本高且推理仅 ~200ms, 不建议。

---

## 9. 验收标准 (回归基线 = 本机 whisper 版实测)

| 测试 | 命令 | 通过 | 实测 (2026-10-27, SenseVoice 部署, CPU 8 核 15GB)
|---|---|---|---|
| 文本回路 5 轮 × 4 句 | `.venv/bin/python bench_industrial.py 5 text` | 正确率 100% (20/20), 路径全 FAST, E2E mean < 300 ms | ✅ 20/20 FAST, Laya mean 200 ms, E2E mean 200 ms (p95 324 ms) |
| 语音回路 5 轮 × 4 句 | `.venv/bin/python bench_industrial.py 5 voice` | 正确率 100% (20/20), E2E mean < 2.0 s | ✅ 20/20 FAST, ASR mean **59 ms** (sherpa-onnx 常驻), Laya mean 232 ms, E2E mean **292 ms** (p95 435 ms) [2026-10-27 复测; 旧 voxtype-CLI 分支: ASR 784 ms / E2E 974 ms] |
| 超纲降级 ① | 输入 "把三号口的窗户关上" | 落 PAUSE+HUMAN | ✅ PAUSE+HUMAN (LLM 无输出) |
| 超纲降级 ② | 输入 "不要香蕉" | 落 PAUSE+HUMAN (幻觉项被拦截) | ✅ PAUSE+HUMAN (LLM 幻觉项被拦截: 苹果/香蕉) |
| 单意图快路径 | 输入 "给我来十个苹果" | FAST, 数量=10 | ✅ FAST qty=10, 72 ms |

超纲用例可复跑: `LLM_MODEL=qwen3.5:4b .venv/bin/python bench_oob.py` (3/3 PASS)。

语音验收参考值: **ASR 常驻引擎 (sherpa-onnx 进程内)**: ASR mean < 100 ms, E2E mean < 500 ms (实测 292 ms, Laya 段占大头)。
若退回 voxtype-CLI 分支: ASR ~0.8 s/句 (冷启动开销), E2E ~1.0 s。见 §10 踩坑清单。
若语音正确率 < 100%, 先查 §7 同音字表, 再决定是否放宽。

---

## 10. 踩坑清单 (本机已验证, 部署时直接规避)

| 坑 | 规避 |
|---|---|
| ollama 思考模型空响应/超时 | 所有 LLM 请求体带 `"think": false` |
| 4b 自动 offload 只上 ~42% GPU (iGPU 共享内存按系统空闲算, offload 保守) | 请求体 `options.num_gpu=99` 强制全层; `ollama ps` 应显示 `100% GPU` |
| 4b 对越纲句返回 `{}` (无 items 键) 而非 `{"items":[]}` | `llm_bypass` 把空对象归一化为空列表, 仍落 PAUSE+HUMAN |
| 多个 bench 并发把 ollama 拖死 (CPU 0.2% 假死) | `run_all.sh` 锁文件; 部署回归一次只跑一个 |
| HF / hf-mirror 不通 | Laya 检查点: hf-mirror 或 rsync 本机; ASR: modelscope/voxtype |
| voxtype 输入格式 | 必须 16 kHz mono WAV; edge-tts 的 mp3 先 ffmpeg 转码 |
| espeak 机器音 | 测试语音一律 edge-tts 自然音 |
| Laya 多问题单请求裸词漂移 | 保持"每提及单独请求 + state 写成 `用户要货: X x N`" 的问法, 勿改 |
| 计时偏差 | 若仍保留 faster-whisper 分支: `transcribe()` 是惰性生成器, 消费完再计时 |
| 置信度虚高 | 校验器是硬约束, 永远不要退化成"置信度阈值兜底" |
| ASR 走 voxtype CLI 每次冷启动 ~750ms (进程+模型加载), 远慢于 voxtype 交互 (daemon 常驻模型) | `bench_industrial.asr()` 改走 sherpa-onnx 进程内常驻引擎 (模型只加载一次 ~0.7s), CLI 仅回退; 实测 ASR 784ms→59ms |
| sherpa-onnx SenseVoice 的 ITN 有 bug ("十"→"1", "十个"→"十0个") | 用 `use_itn=False` 出中文数字 (规则层本就解析"十个"), `language="zh"` 锁语种避免短句误判成日语 |
| 无标点文本下否定词跨句误匹配 ("不要梨五个香蕉" 误伤香蕉) | 否定正则: 否定词与宾语间 ≤3 字且不得跨另一个水果名 `(?:(?!苹果|香蕉|梨).){0,3}` |

---

## 11. 部署步骤 (顺序)

1. `omarchy pkg add git ffmpeg sox python uv ollama` + `omarchy pkg aur add voxtype-bin`
2. `git clone https://github.com/NandhaKishorM/laya ~/Work/laya` (checkout `3cf26cb`) 或 rsync 本机仓库 (含本地实验脚本)
3. 建 venv + 装依赖 (§4)
4. `voxtype setup` 拉 SenseVoiceSmall; `ollama pull qwen3.5:4b`
5. 拉 Laya 检查点到 `models/laya/` 并建符号链接 (§5)
6. 生成 `test_audio/tts_1..4.wav` (§6)
7. 改 `bench_industrial.py` ASR 分支 (§7), 补 SenseVoice 同音字表
8. 起 jev_api, 健康检查
9. 跑文本回归 (20/20) → 跑语音回归 (20/20) → 3 条超纲用例 (§9)
10. 全绿后记录 E2E 分段数据, 更新本报告 §9 实测列
