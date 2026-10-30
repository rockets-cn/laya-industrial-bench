"""工业决策回路 Web UI (DEPLOY_SENSEVOICE 部署的测试页).

语音/文本 -> ASR(SenseVoice/voxtype) -> 规则归一化 -> Laya(:8000) -> 校验器 -> (LLM旁路:11434)
纯转发进程, 不加载任何模型. 复用 bench_industrial.py 的 pipeline 函数.

Run: .venv/bin/python loop_ui.py            # 127.0.0.1:8002
"""
import importlib.util
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("LLM_MODEL", "qwen3.5:4b")  # bench_pipeline reads it at import time
spec = importlib.util.spec_from_file_location("bi", os.path.join(HERE, "bench_industrial.py"))
bi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bi)

# 彩色方块域 (bench_block.py): 同架构, 词表换成 红/绿/黄/蓝 方块
spec_bb = importlib.util.spec_from_file_location("bb", os.path.join(HERE, "bench_block.py"))
bb = importlib.util.module_from_spec(spec_bb)
spec_bb.loader.exec_module(bb)

# 料场分拣域 (bench_yard.py): 1/2/3 号料场 × 8 种物料, 语音指令分拣下发
spec_by = importlib.util.spec_from_file_location("by", os.path.join(HERE, "bench_yard.py"))
by = importlib.util.module_from_spec(spec_by)
spec_by.loader.exec_module(by)

# 本机问答兜底 (local_qa.py): 时间/服务端口/资源/料场库存, 纯规则层零 LLM, 答案<=30字
spec_lq = importlib.util.spec_from_file_location("local_qa", os.path.join(HERE, "local_qa.py"))
local_qa = importlib.util.module_from_spec(spec_lq)
spec_lq.loader.exec_module(local_qa)

# 出入库台账 (yard_ledger.py, SQLite): 分拣下发自动记入, 页面手动记出, GET /ledger
spec_yl = importlib.util.spec_from_file_location("yard_ledger", os.path.join(HERE, "yard_ledger.py"))
yard_ledger = importlib.util.module_from_spec(spec_yl)
spec_yl.loader.exec_module(yard_ledger)

import bench_pipeline  # noqa: E402  (already imported by bench_industrial)
bench_pipeline.MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b")

# 分拣线光门安装示意: 优先 动态版 (gate_layout_anim.svg, 按真实时序驱动: 带速 1.0m/s/
# 光门落沿触发推杆 400ms 行程/D=v×500ms 对到推料点/拨入 45°料道, 循环演示, 尊重
# prefers-reduced-motion); 动态版缺失时回落 静态版 (gate_layout.svg, 打印/导出用)
_gate_path = os.path.join(HERE, "edge101", "docs", "gate_layout_anim.svg")
if not os.path.exists(_gate_path):
    _gate_path = os.path.join(HERE, "edge101", "docs", "gate_layout.svg")
GATE_SVG = open(_gate_path, encoding="utf-8").read()

app = FastAPI(title="Laya 工业决策回路")

AUDIO_DIR = os.path.join(HERE, "test_audio")
# (名称, 文本, 领域 fruit|block|mixed)
PRESETS = [
    ("c1 多意图", "我要那个苹果，我要那个梨，给我十个香蕉", "fruit"),
    ("c2 数量", "来两个红苹果和一把香蕉", "fruit"),
    ("c3 单意图", "只给我三个梨就行", "fruit"),
    ("c4 否定", "一个苹果，不要梨，五个香蕉谢谢", "fruit"),
    ("超纲① 关窗", "把三号口的窗户关上", "fruit"),
    ("超纲② 纯否定", "不要香蕉", "fruit"),
    ("快路径 十个", "给我来十个苹果", "fruit"),
    ("方块 例", "10个红色方块，5个绿色方块，7个黄色方块", "block"),
    ("方块 否定", "8个黄色方块，不要绿色方块", "block"),
    ("方块 超纲", "就给我3个方块", "block"),
    ("混合 水果+方块", "两个红苹果和10个红色方块，再来3个蓝色方块", "mixed"),
    ("仓库 双场双料", "给1号仓库送两个红苹果，3号仓库送10个蓝色方块", "yard"),
    ("仓库 倒序", "两个红苹果送到3号仓库，1号仓库来五个香蕉", "yard"),
    ("仓库 否定", "1号仓库送四个梨，不要绿色方块", "yard"),
    ("仓库 超纲 4号", "给4号仓库送两个苹果", "yard"),
    ("仓库 超纲 缺场", "送两个红苹果", "yard"),
    ("仓库 密集无标点", "一号仓库一个香蕉两个苹果二号仓库三个苹果四个香蕉三号仓库一个梨一个香蕉", "yard"),
]

SIDES = {"fruit": bi, "block": bb, "yard": by}


def run_side(side, text):
    """单领域跑一圈: 规则层 -> (分配展开) -> Laya -> 校验器 -> (LLM旁路). side: fruit|block|yard."""
    ns = SIDES[side]
    norm, mentions, neg = ns.normalize(text)
    dist_ms = 0.0
    if not mentions and not neg:
        dexp = getattr(ns, "dist_expand_if_needed", None)
        if dexp:
            dm, dt_d, _dr = dexp(norm) or (None, 0.0, [])
            if dm:
                mentions, dist_ms = dm, dt_d
    if not mentions and not neg:
        return {"side": side, "empty": True, "normalized": norm, "negated": neg,
                "mentions": [], "laya_ms": 0.0, "answers": {}, "ok": True,
                "actions": [], "reasons": [], "path": "EMPTY", "bypass_ms": 0.0,
                "dist_ms": 0.0, "bypass": None}
    dt_l, answers = ns.laya_decide(mentions) if mentions else (0.0, {})
    ok, actions, reasons = ns.validate(mentions, answers)
    path, dt_b, bypass = ("EXPAND" if dist_ms else "FAST"), 0.0, None
    if not ok:
        try:
            dt_b, b_mentions, b_reasons = ns.llm_bypass(norm, mentions)
            if b_mentions:
                ok_b, actions, reasons = ns.validate(b_mentions, None)
            else:
                ok_b, actions, reasons = False, [], b_reasons or ["LLM 无输出"]
            path = "BYPASS" if ok_b else "PAUSE+HUMAN"
        except Exception as e:
            path, actions, reasons = "PAUSE+HUMAN", [], [f"LLM 不可用: {e}"]
    elif reasons:
        # ok=True + 仅超纲挂起说明: 在纲项照发, 超纲项报人工 (部分下发)
        path = "PARTIAL"
    return {"side": side, "empty": False, "normalized": norm, "negated": neg,
            "mentions": mentions, "laya_ms": dt_l, "answers": answers, "ok": ok,
            "actions": actions, "reasons": reasons, "path": path,
            "bypass_ms": dt_b, "dist_ms": dist_ms, "bypass": bypass}


def run_pipeline(text, asr_dt=0.0, asr_text=None, last_wav=None, domain="fruit", qa_llm=None):
    """Text/normalized -> 问答兜底 -> (规则层 -> Laya -> 校验器 -> LLM旁路). Returns full trace dict.
    domain: fruit | block | mixed (两个规则层并行, 各抽各的, 动作合并).
    qa_llm: LLM 问答兜底开关 — None=用环境变量默认 (QA_LLM=1), True/False=本次请求指定
            (页面「LLM 兜底」开关, 运行时切换, 无需重启服务)."""
    t0 = time.perf_counter()
    if domain == "yard":
        # 问答: 先规则层 (毫秒级/确定性); miss 且 LLM 兜底开启 (页面开关或 QA_LLM=1) 时
        # 再让 LLM 基于注入事实答长尾 — 失败/未开则照旧回落分拣路径。
        qhit, qans, _qdt = local_qa.answer(text)
        qsrc = "rule"
        llm_on = bool(qa_llm) if qa_llm is not None else local_qa.llm_enabled()
        if not qhit and llm_on:
            qhit, qans, _qllm = local_qa.answer_llm(text)
            qsrc = "llm"
        if qhit:
            return {"domain": domain, "asr_text": asr_text if asr_text is not None else text,
                    "asr_model": getattr(bi, "LAST_ASR_MODEL", "?"), "last_wav": last_wav,
                    "asr_ms": round(asr_dt * 1000), "normalized": text, "negated": [],
                    "mentions": [], "laya_ms": 0, "laya_answers": {},
                    "validator_ok": True, "actions": [], "reject_reasons": [],
                    "bypass_ms": 0, "bypass": None, "path": "QA", "answer": qans,
                    "qa_source": qsrc,
                    "dist_total": None, "dist_ms": 0,
                    "e2e_ms": round((time.perf_counter() - t0) * 1000)}
    sides = ["fruit", "block"] if domain == "mixed" else [domain]
    results = [run_side(s, text) for s in sides]
    active = [r for r in results if not r["empty"]]
    if not active:
        results = [{"side": sides[0], "empty": False, "normalized": text, "negated": [],
                    "mentions": [], "laya_ms": 0.0, "answers": {}, "ok": False,
                    "actions": [], "reasons": ["空订单"], "path": "PAUSE+HUMAN",
                    "bypass_ms": 0.0, "bypass": None}]
        active = results
    order = {"QA": 0, "FAST": 0, "PARTIAL": 0, "EXPAND": 1, "BYPASS": 1, "PAUSE+HUMAN": 2}
    path = max((r["path"] for r in active), key=lambda p: order[p])
    merged_answers = {}
    pfx = {"fruit": "f_", "block": "b_", "yard": "y_"}
    for r in results:
        for k, v in r["answers"].items():
            merged_answers[pfx[r["side"]] + k] = v
    merged_bypass = {k: v for r in results for k, v in
                     (r["bypass"].items() if r["bypass"] else [])} or None
    _res = {
        "domain": domain,
        "asr_text": asr_text if asr_text is not None else text,
        "asr_model": getattr(bi, "LAST_ASR_MODEL", "?"),
        "last_wav": last_wav,
        "asr_ms": round(asr_dt * 1000),
        "normalized": " | ".join(f"[{r['side']}] {r['normalized']}" for r in results)
                      if domain == "mixed" else results[0]["normalized"],
        "negated": [n for r in results for n in r["negated"]],
        "mentions": [dict(m, side=r["side"]) for r in results for m in r["mentions"]],
        "laya_ms": round(sum(r["laya_ms"] for r in results) * 1000),
        "laya_answers": {k: {"choice": v.get("choice"),
                             "probabilities": {n: round(p, 3) for n, p in v.get("probabilities", {}).items()},
                             "confidence": round(v.get("confidence") or 0, 3)}
                         for k, v in merged_answers.items()},
        "validator_ok": all(r["ok"] for r in active),
        "actions": [a for r in results for a in r["actions"]],
        "reject_reasons": [f"[{r['side']}]" + rr for r in results for rr in r["reasons"]],
        "bypass_ms": round(max((r["bypass_ms"] for r in results), default=0.0) * 1000),
        "bypass": merged_bypass,
        "path": path,
        "dist_total": next((m.get("dist_total") for r in results
                            for m in r["mentions"] if m.get("dist_total")), None),
        "dist_ms": round(max((r.get("dist_ms", 0.0) for r in results), default=0.0) * 1000),
        "e2e_ms": round((time.perf_counter() - t0) * 1000),
    }
    local_qa.set_last_order(_res["actions"])  # 问答兜底用: 最近一次分拣库存
    # 出入库台账: 产生可执行下发 (FAST/EXPAND/PARTIAL) 即自动记「入」—
    # 统计 各料场 放入了什么/多少件; PAUSE/QA/空订单 无动作, 不记账。
    if _res["path"] in ("FAST", "EXPAND", "PARTIAL") and _res["actions"]:
        try:
            yard_ledger.record_in(_res["actions"], _res["path"], source="loop_ui")
        except Exception:
            pass  # 记账失败不影响主流程 (台账是统计, 不是下发路径)
    return _res


def _asr_from_body(body):
    """/asr 与 /run 共用的 ASR 段: wav (16k mono b64) 或 mic (浏览器 webm b64 → ffmpeg)
    -> (dt_s, text, last_wav); 无音频返回 None, 出错抛 HTTPException."""
    wav, mic = body.get("wav"), body.get("mic")
    asr_dt, text, last_wav = 0.0, None, None
    if wav:
        try:
            import base64
            raw = base64.b64decode(wav)
        except Exception:
            raise HTTPException(422, "bad wav")
        p = os.path.join(AUDIO_DIR, "_upload.wav")
        with open(p, "wb") as f:
            f.write(raw)
        try:
            asr_dt, text = bi.asr(p)
        except Exception as e:
            raise HTTPException(500, f"ASR failed: {e}")
        import shutil
        shutil.copy(p, os.path.join(AUDIO_DIR, "last_asr_input.wav"))
        last_wav = "last_asr_input.wav"
    elif mic:
        # Browser MediaRecorder stream (webm/opus or similar) -> 16k mono wav
        try:
            import base64
            raw = base64.b64decode(mic)
        except Exception:
            raise HTTPException(422, "bad mic data")
        src = os.path.join(AUDIO_DIR, "_mic.webm")
        wavp = os.path.join(AUDIO_DIR, "_mic.wav")
        with open(src, "wb") as f:
            f.write(raw)
        conv = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                                "-ar", "16000", "-ac", "1", wavp],
                               capture_output=True, text=True, timeout=120)
        if conv.returncode != 0 or not os.path.exists(wavp):
            raise HTTPException(500, f"audio convert failed: {conv.stderr[:200]}")
        try:
            asr_dt, text = bi.asr(wavp)
        except Exception as e:
            raise HTTPException(500, f"ASR failed: {e}")
        import shutil
        shutil.copy(wavp, os.path.join(AUDIO_DIR, "last_asr_input.wav"))
        last_wav = "last_asr_input.wav"
    if text is None:
        return None
    return asr_dt, text, last_wav


@app.post("/asr")
async def asr_only(request: Request):
    """只做 ASR (不跑 Laya/LLM 管线) — 页面拿到 识别文字 立即显示;
    随后 /run 携带 asr_text 复用本结果, 不重复识别. 出错时页面回落单发 /run(wav)."""
    body = await request.json()
    r = _asr_from_body(body)
    if r is None:
        raise HTTPException(422, "wav or mic required")
    dt, text, last_wav = r
    return {"text": text, "asr_ms": round(dt * 1000), "last_wav": last_wav,
            "asr_model": getattr(bi, "LAST_ASR_MODEL", "?")}


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    wake = bool(body.get("wake"))
    domain = body.get("domain") or "fruit"
    if domain not in ("fruit", "block", "mixed", "yard"):
        domain = "fruit"
    qa_llm = body.get("qa_llm")  # 页面「LLM 兜底」开关: true/false, 缺省=环境变量默认
    asr_dt, asr_text = 0.0, None
    last_wav = None
    if "asr_text" in body and body.get("asr_text"):
        # 两段式: 页面已先调 /asr (即时显示识别文字), /run 复用结果 — 不重复 ASR
        text = str(body.get("asr_text")).strip()
        last_wav = body.get("last_wav")
        try:
            asr_dt = max(0.0, float(body.get("asr_ms") or 0)) / 1000.0
        except (TypeError, ValueError):
            asr_dt = 0.0
    elif body.get("wav") or body.get("mic"):
        r = _asr_from_body(body)  # 单发路径 (含 /asr 失败回落): ASR + 管线一次完成
        if r is None:
            raise HTTPException(422, "text, wav or mic required")
        asr_dt, text, last_wav = r
    elif not text:
        raise HTTPException(422, "text, wav or mic required")
    if wake:
        # 唤醒词检测: 只做 ASR, 不跑 Laya/LLM 管线
        return {"wake": True, "asr_text": text, "asr_ms": round(asr_dt * 1000),
                "asr_model": getattr(bi, "LAST_ASR_MODEL", "?")}
    return run_pipeline(text, asr_dt, None, last_wav, domain=domain, qa_llm=qa_llm)


@app.get("/presets")
def presets():
    return [{"name": n, "text": t, "domain": d} for n, t, d in PRESETS]


# ---- 出入库台账 (yard_ledger.py, SQLite: data/yard_ledger.db) ----
@app.get("/stock")
def stock_get():
    """当前库存 (入-出, 不含流水) — 页面「常驻库存条」用, 比 /ledger 轻."""
    return yard_ledger.stock()


@app.get("/ledger")
def ledger_get():
    """当前库存 (入-出) + 最近流水 — 页面「📦 仓库台账」面板用."""
    d = yard_ledger.stock()
    d["recent"] = yard_ledger.recent(20)
    return d


@app.post("/ledger/out")
async def ledger_out(request: Request):
    """手动出库 (页面表单): {yard:1-3, material:白名单, qty:1-999, note?}."""
    body = await request.json()
    try:
        yard_ledger.record_out(int(body.get("yard", 0)),
                               str(body.get("material", "")),
                               int(body.get("qty", 0)),
                               note=str(body.get("note") or "手动出库"))
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@app.post("/ledger/reset")
def ledger_reset():
    """清空整个台账 (页面「🗑 清空台账」按钮 / 命令行). 服务无需停止."""
    yard_ledger.reset()
    return {"ok": True}


# MQTT 分拣网关 (sort_mqtt.py, 默认 :8123): 把 Laya 分类结果下发到分拣线
GATEWAY_URL = os.environ.get("SORT_GATEWAY_URL", "http://127.0.0.1:8123")


@app.post("/dispatch")
async def dispatch(request: Request):
    """把最近一次 /run 的 yard actions 转发到分拣网关 (白名单校验在网关侧)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "bad json")
    actions = body.get("actions")
    if not isinstance(actions, list) or not actions:
        raise HTTPException(422, "actions required")
    req = urllib.request.Request(
        GATEWAY_URL + "/dispatch",
        data=json.dumps({"actions": actions, "source": "loop_ui",
                          "path": body.get("path")}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            # 透传网关状态码 (协议契约 202=入队; 用 dict return 会被 FastAPI 压成 200)
            return JSONResponse(json.loads(r.read().decode()),
                                 status_code=r.status)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode() or "{}")
        except Exception:
            detail = {}
        raise HTTPException(e.code, detail.get("error", f"gateway error {e.code}"))
    except Exception as e:
        raise HTTPException(503, f"分拣网关不可达 ({GATEWAY_URL}): {e}")


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Laya 仓库分拣回路 v13</title>
<style>
 body{font-family:system-ui,'Noto Sans CJK SC',sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 .wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px}
 h1{font-size:18px;font-weight:600;margin:0}
 .ver{color:#6b7280;font-size:12px;font-weight:400;margin-left:8px}
 .cards{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
 .card{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:10px 14px;flex:1;min-width:110px}
 .card .k{font-size:11px;color:#8a91a0} .card .v{font-size:19px;font-weight:700;margin-top:3px}
 .path-FAST{color:#4ade80}.path-QA{color:#4ade80}.path-PARTIAL{color:#fb923c}.path-BYPASS{color:#facc15}.path-EXPAND{color:#facc15}.path-PAUSE+HUMAN{color:#f87171}
 .row{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;align-items:center}
 button{background:#1e2430;color:#e6e6e6;border:1px solid #2c3342;border-radius:8px;padding:8px 12px;
  cursor:pointer;font-size:13px} button:hover{background:#262d3d}
 button:disabled{opacity:.45;cursor:default}
 button.primary{background:#2563eb;border-color:#2563eb;color:#fff;font-weight:600}
 input#txt{flex:1;min-width:240px;background:#171a21;color:#e6e6e6;border:1px solid #262b36;
  border-radius:8px;padding:9px 12px;font-size:14px}
 select{background:#171a21;color:#e6e6e6;border:1px solid #262b36;border-radius:8px;padding:8px 10px;font-size:13px}
 .drop{border:1px dashed #3a4152;border-radius:10px;padding:12px;color:#8a91a0;font-size:13px;
  cursor:pointer;display:inline-block}
 .drop.on{border-color:#4ade80;color:#4ade80}
 pre{background:#12141a;border:1px solid #262b36;border-radius:10px;padding:12px;font-size:12.5px;
  overflow:auto;margin:8px 0;white-space:pre-wrap}
 .sec{font-size:12px;color:#8a91a0;margin:14px 0 4px;text-transform:uppercase;letter-spacing:.05em}
 .chip{display:inline-block;background:#1e2430;border-radius:6px;padding:2px 8px;font-size:11px;
  color:#8a91a0;margin-left:6px;text-transform:none;letter-spacing:0}
 .yd{font-size:12px;color:#8a91a0;margin-top:4px;line-height:1.7}
 .muted{color:#6b7280;font-size:12px}
 .bar{height:6px;background:#262b36;border-radius:3px;overflow:hidden;margin-top:6px}
 .bar i{display:block;height:100%;width:0;background:#4ade80;transition:width .08s}
 details.fold{margin:10px 0}
 details.fold>summary{cursor:pointer;color:#8a91a0;font-size:13px;user-select:none}
 details.fold>summary:hover{color:#c3c8d4}
 #flow{width:100%;height:auto;display:block;margin:12px 0 0}
 #flow .n rect{fill:#12141a;stroke:#232836}
 #flow .n text{fill:#565d6d;font-size:12.5px}
 #flow .n .sub{fill:#4b5563;font-size:9px}
 #flow .n .ms{fill:#6b7280;font-size:10.5px}
 #flow .n.on rect{fill:#0b2417;stroke:#2f9e63}
 #flow .n.on text{fill:#4ade80}
 #flow .n.on .sub{fill:#2f9e63}
 #flow .n.warn rect{fill:#2a2410;stroke:#c7a13a}
 #flow .n.warn text{fill:#facc15}
 #flow .n.warn .sub{fill:#c7a13a}
 #flow .n.stop rect{fill:#2a1214;stroke:#b3454f}
 #flow .n.stop text{fill:#f87171}
 #flow .n.off rect{stroke-dasharray:4 3}
 #flow .edge{stroke:#3a4152}
</style></head><body><div class="wrap">
<h1>🏭 Laya 工业决策回路 · 仓库分拣<span class="ver">v13 · 分拣+简单问答(≤30字)+部分下发+LLM兜底(可开关)+仓库台账(SQLite)+常驻库存卡 · 失败降级 暂停+人工</span></h1>
<svg id="flow" viewBox="0 0 900 84" xmlns="http://www.w3.org/2000/svg">
 <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#3a4152"/></marker></defs>
 <line class="edge" x1="108" y1="32" x2="130" y2="32" marker-end="url(#arr)"/>
 <line class="edge" x1="240" y1="32" x2="262" y2="32" marker-end="url(#arr)"/>
 <line class="edge" x1="372" y1="32" x2="394" y2="32" marker-end="url(#arr)"/>
 <line class="edge" x1="504" y1="32" x2="526" y2="32" marker-end="url(#arr)"/>
 <line class="edge" x1="636" y1="32" x2="658" y2="32" marker-end="url(#arr)"/>
 <line class="edge" x1="768" y1="32" x2="790" y2="32" marker-end="url(#arr)"/>
 <g id="fn1" class="n off"><rect x="2" y="14" width="104" height="36" rx="9"/><text x="54" y="37" text-anchor="middle">语音/文本</text></g>
 <g id="fn2" class="n off"><rect x="134" y="14" width="104" height="36" rx="9"/><text x="186" y="29" text-anchor="middle">ASR</text><text x="186" y="43" text-anchor="middle" class="sub">SenseVoice</text><text id="fn-asr" x="186" y="66" text-anchor="middle" class="ms">—</text></g>
 <g id="fn3" class="n off"><rect x="266" y="14" width="104" height="36" rx="9"/><text x="318" y="29" text-anchor="middle">规则归一化</text><text x="318" y="43" text-anchor="middle" class="sub">抽取 · 归属</text></g>
 <g id="fn4" class="n off"><rect x="398" y="14" width="104" height="36" rx="9"/><text x="450" y="29" text-anchor="middle">Laya 决策</text><text x="450" y="43" text-anchor="middle" class="sub">物料终审</text><text id="fn-laya" x="450" y="66" text-anchor="middle" class="ms">—</text></g>
 <g id="fn5" class="n off"><rect x="530" y="14" width="104" height="36" rx="9"/><text x="582" y="29" text-anchor="middle">校验器</text><text x="582" y="43" text-anchor="middle" class="sub">白名单 · 去重</text></g>
 <g id="fn6" class="n off"><rect x="662" y="14" width="104" height="36" rx="9"/><text x="714" y="29" text-anchor="middle">LLM 旁路</text><text x="714" y="43" text-anchor="middle" class="sub">校验拒绝 · 分配展开</text><text id="fn-bypass" x="714" y="66" text-anchor="middle" class="ms">未触发</text></g>
 <g id="fn7" class="n off"><rect x="794" y="14" width="104" height="36" rx="9"/><text x="846" y="29" text-anchor="middle">PLC 下发</text><text x="846" y="43" text-anchor="middle" class="sub">暂停 = 不发</text><text id="fn-e2e" x="846" y="66" text-anchor="middle" class="ms">—</text></g>
</svg>
<div class="row">
 <select id="preset" onchange="runPresetSel()" title="测试指令预设"><option value="" selected>预设指令…</option></select>
 <input id="txt">
 <button class="primary" onclick="runText()">▶ 运行</button>
 <button id="btnDispatch" onclick="dispatchRun()" disabled title="把最近一次运行的分类结果下发到 MQTT 分拣线">📤 下发分拣线</button>
 <label style="display:flex;align-items:center;gap:5px;font-size:13px;color:#8a91a0;cursor:pointer" title="规则层认不出的问句交给 LLM 基于注入事实回答 (~10s, 仅展示不下发); 规则层能答的仍毫秒级直答; 开关状态保存在本浏览器"><input type="checkbox" id="qallm"> LLM 兜底</label>
 <button onclick="toggleLedger()" title="SQLite 出入库台账: 分拣下发自动记入, 这里可手动记出">📦 仓库台账</button>
 </div>
<div class="row">
 <button id="micbtn" class="primary" style="min-width:120px" onclick="authToggle()">🎤 授权麦克风</button>
 <button id="ptt" title="按住说话 (或按住 空格键)" style="min-width:120px;touch-action:none;user-select:none" onpointerdown="pttDown(event)" onpointerup="pttUp()" onpointerleave="pttUp()" onpointercancel="pttUp()" oncontextmenu="event.preventDefault();return false" disabled>🗣 按住说话</button>
 <label style="display:flex;align-items:center;gap:5px;font-size:13px;color:#8a91a0;cursor:pointer"><input type="checkbox" id="wakesw" onchange="wakeToggle()" disabled> 唤醒词「优优」</label>
 <div style="flex:1;min-width:160px"><div id="micstate" class="muted"></div><div class="bar"><i id="lv"></i></div></div>
</div>
<div id="cards" class="cards"></div>
<div id="stockcards" class="cards" style="margin:10px 0 4px"></div>
<div id="yardcards" class="cards"></div>
<div id="ledger" style="display:none;margin:14px 0"></div>
<div id="out"></div>
<details class="fold" id="trace"><summary>全链路 trace (ASR 原文 · 规则层 · Laya 概率 · 校验器 · 旁路)</summary><div id="tracebody"></div></details>
<details class="fold" id="gate"><summary>📐 分拣线光门安装布局（俯视 · 每路推杆前侧/上游 · 动态演示 · 物理参考, 装前必看）</summary>
<div style="background:#12141a;border:1px solid #262b36;border-radius:10px;padding:10px 12px;margin:8px 0;overflow-x:auto">
@GATE_SVG@
<table style="width:100%;border-collapse:collapse;font-size:12.5px;margin:8px 0 4px">
<tr><th style="border:1px solid #262b36;padding:6px 8px;text-align:left;color:#8a91a0">光门</th><th style="border:1px solid #262b36;padding:6px 8px;text-align:left;color:#8a91a0">对应推杆</th><th style="border:1px solid #262b36;padding:6px 8px;text-align:left;color:#8a91a0">MCP23017</th><th style="border:1px solid #262b36;padding:6px 8px;text-align:left;color:#8a91a0">安装位置（俯视）</th><th style="border:1px solid #262b36;padding:6px 8px;text-align:left;color:#8a91a0">安装高度（侧视）</th></tr>
<tr><td style="border:1px solid #262b36;padding:6px 8px">光门 1（透过式 NPN 开集）</td><td style="border:1px solid #262b36;padding:6px 8px">1 号推杆</td><td style="border:1px solid #262b36;padding:6px 8px;color:#fb923c">PB0</td><td style="border:1px solid #262b36;padding:6px 8px">推杆 1 推料点<b>前方 D</b>（D = v×(推出400ms+余量100ms) ≈ 50cm @1m/s），光束横跨料道、垂直带面</td><td style="border:1px solid #262b36;padding:6px 8px">物品质心高（离带面 10~30mm）</td></tr>
<tr><td style="border:1px solid #262b36;padding:6px 8px">光门 2</td><td style="border:1px solid #262b36;padding:6px 8px">2 号推杆</td><td style="border:1px solid #262b36;padding:6px 8px;color:#fb923c">PB1</td><td style="border:1px solid #262b36;padding:6px 8px">推杆 2 推料点前方 D（同上）</td><td style="border:1px solid #262b36;padding:6px 8px">同上</td></tr>
<tr><td style="border:1px solid #262b36;padding:6px 8px">光门 3</td><td style="border:1px solid #262b36;padding:6px 8px">3 号推杆</td><td style="border:1px solid #262b36;padding:6px 8px;color:#fb923c">PB2</td><td style="border:1px solid #262b36;padding:6px 8px">推杆 3 推料点前方 D（同上）</td><td style="border:1px solid #262b36;padding:6px 8px">同上</td></tr>
</table>
<ul style="font-size:12.5px;line-height:1.55;color:#8a91a0;padding-left:18px;margin:4px 0">
<li><b>为什么是前侧</b>：光门落沿只是「推出动作开始」的时刻——推杆 400ms 推出行程必须赶在物品到达推杆推料点之前完成，所以门装在推杆上游 D 处；装在后面物品已越过推料点，触发无效。</li>
<li><b>件距约束</b>：相邻两件 ≥ v×0.9s（1m/s → ≥0.9m，图中光门 1↔2 的 270px），上游排产保证，否则第二件到门时第一件还没推出 → 漏拨。</li>
<li><b>末端对账门 ≠ 时序门</b>：料道末端（图中虚线 ×，OPEN #3②）确认件真进道（长期计数），与本次 3 路前侧时序门职责不同，本次不装。</li>
<li><b>启用顺序</b>：接线 → 串口 <code>gates</code> 确认 ok=1/挡光变 0 → 按 ⑧ 带速标定修正 D → 网关 <code>divert_trigger</code> 改 "photo"；未接线期间保持 "immediate"（固件自动回退，实机已验证）。</li>
</ul>
</div>
</details>
<script>
const $=id=>document.getElementById(id);
window.addEventListener('error',e=>{const m=$('micstate');
 if(m)m.textContent='JS错误: '+(e.message||'unknown')+' @'+(e.lineno||'?');
 console.error('PAGE JS ERROR',e.message,e.stack)});
window.addEventListener('unhandledrejection',e=>{const m=$('micstate');
 if(m)m.textContent='Promise错误: '+(e.reason&&e.reason.message||e.reason);console.error('UNHANDLED',e.reason)});
async function jpost(u,d){const r=await fetch(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(d)});
 if(!r.ok)throw new Error((await r.text()).slice(0,300));return r.json()}
function card(k,v,cls){return `<div class="card"><div class="k">${k}</div><div class="v ${cls||''}">${v}</div></div>`}
function sec(t){return `<div class="sec">${t}</div>`}
const domain='yard'; // 页面专属仓库分拣 (后端 API 仍支持 fruit/block/mixed 基准域; 域标识保持 yard 不变)
const LEDGER_MAT={red_apple:'红苹果',green_apple:'绿苹果',banana:'香蕉',pear:'梨',red_block:'红色方块',green_block:'绿色方块',yellow_block:'黄色方块',blue_block:'蓝色方块'};
// ---- 常驻库存 (GET /stock; 仓库信息之上, 每仓一个卡, 样式与仓库信息一致; 不用点台账面板) ----
async function refreshStock(){
 try{
  const d=await (await fetch('/stock')).json();
  let h='';
  [1,2,3].forEach(y=>{
   const it=d.yards[y].items,tot=d.yards[y].total;
   const rows=Object.entries(it).filter(([,v])=>v!==0).map(([m,v])=>
    `<div class="yd">${v<0?`<span style="color:#f87171">${LEDGER_MAT[m]||m} × ${v}</span>`:(LEDGER_MAT[m]||m)+' × '+v}</div>`).join('')
    ||'<div class="yd">—</div>';
   h+=`<div class="card"><div class="k">${y}号仓库 库存</div>`
    +`<div class="v ${tot>0?'':(tot<0?'':'muted')}"${tot<0?' style="color:#f87171"':''}>${tot>0?tot+' 件':(tot<0?'负 '+Math.abs(tot)+' 件':'空')}</div>${rows}</div>`;
  });
  h+=`<div class="card" style="min-width:90px"><div class="k">📦 总库存</div>`
   +`<div class="v ${d.grand_total>0?'':'muted'}">${d.grand_total>0?d.grand_total+' 件':'0 件'}</div></div>`;
  $('stockcards').innerHTML=h;
 }catch(e){$('stockcards').innerHTML='<div class="card"><div class="k">📦 库存</div><div class="v muted">加载失败 '+(e.message||e)+'</div></div>'}
}
// ---- 仓库台账 (yard_ledger.py, SQLite): 库存=入-出; 分拣下发自动记入, 面板内可手动记出 ----
let ledOpen=false;
function toggleLedger(){ledOpen=!ledOpen;$('ledger').style.display=ledOpen?'block':'none';if(ledOpen)refreshLedger();}
async function refreshLedger(){
 try{
  const d=await (await fetch('/ledger')).json();
  let yc='';[1,2,3].forEach(y=>{
   const it=d.yards[y].items,tot=d.yards[y].total;
   const rows=Object.entries(it).map(([m,v])=>
    `<div class="yd">${v<0?'<span style="color:#f87171">':' '}${LEDGER_MAT[m]||m} × ${v}</div>`).join('')||'<div class="yd">—</div>';
   yc+=`<div class="card"><div class="k">${y}号仓库 库存</div><div class="v ${tot?'':'muted'}">${tot>0?tot+' 件':(tot<0?'负 '+Math.abs(tot):'空')}</div>${rows}</div>`;
  });
  let h='<div class="cards">'+yc+'</div>'
   +sec('📜 最近流水 <span class="chip">'+d.moves+' 笔 · 入=分拣下发自动记 出=手动</span>')
   +'<pre style="font-size:12px">'+(d.recent.length?d.recent.map(r=>
    `${r.ts.slice(5)} ${r.direction==='in'?'入':'<span style="color:#fb923c">出</span>'} ${r.yard}号 ${LEDGER_MAT[r.material]||r.material} ×${r.qty} (${r.path})`).join(String.fromCharCode(10)):'暂无流水 — 跑一条分拣指令试试')+'</pre>'
   +sec('📤 手动出库')+'<div class="row">'
   +'<select id="loyard">'+[1,2,3].map(y=>`<option value="${y}">${y}号</option>`).join('')+'</select>'
   +'<select id="lomat">'+Object.entries(LEDGER_MAT).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')+'</select>'
   +'<input id="loqty" type="number" min="1" max="999" value="1" style="width:72px">'
   +'<button onclick="ledgerOut()">出库</button>'
   +'<button onclick="ledgerReset()" style="margin-left:auto;color:#f87171" title="DELETE 全部流水, 库存归零 (服务无需停止)">🗑 清空台账</button></div>';
  $('ledger').innerHTML=h;refreshStock();
 }catch(e){$('ledger').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
}
async function ledgerOut(){
 const q=+$('loqty').value;
 if(!(q>=1&&q<=999)){alert('数量需 1~999');return}
 try{await jpost('/ledger/out',{yard:+$('loyard').value,material:$('lomat').value,qty:q});
  $('loqty').value=1;refreshLedger();
 }catch(e){alert('出库失败: '+e.message)}
}
async function ledgerReset(){
 if(!confirm('确定清空整个台账？所有流水与库存将归零, 不可恢复。'))return;
 try{await jpost('/ledger/reset',{});refreshLedger();}
 catch(e){alert('清空失败: '+e.message)}
}
function drawFlow(d){
 const set=(id,c)=>{const el=$(id);el.classList.remove('on','off','warn','stop');el.classList.add(c)};
 if(d.path==='QA'){['fn1','fn2','fn3','fn7'].forEach(i=>set(i,'on'));['fn4','fn5','fn6'].forEach(i=>set(i,'off'))}
 else if(d.path==='FAST'||d.path==='PARTIAL'){['fn1','fn2','fn3','fn4','fn5','fn7'].forEach(i=>set(i,'on'));set('fn6','off')}
 else if(d.path==='BYPASS'||d.path==='EXPAND'){['fn1','fn2','fn3','fn4','fn5'].forEach(i=>set(i,'on'));set('fn6','warn');set('fn7',d.path==='EXPAND'?'on':'warn')}
 else{['fn1','fn2','fn3','fn4','fn5'].forEach(i=>set(i,'on'));set('fn6',d.bypass_ms?'warn':'off');set('fn7','stop')}
 $('fn-asr').textContent=d.asr_ms!=null?d.asr_ms+' ms':'—';
 $('fn-laya').textContent=d.laya_ms!=null?d.laya_ms+' ms':'—';
 $('fn-bypass').textContent=d.bypass_ms?d.bypass_ms+' ms':'未触发';
 $('fn-e2e').textContent='E2E '+d.e2e_ms+' ms';
}
async function runText(){
 const t=$('txt').value.trim(); if(!t)return;
 $('out').innerHTML=sec('运行中…');
 try{render(await jpost('/run',{text:t,domain:domain,qa_llm:$('qallm').checked}))}catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
 }
let lastRun=null;
function setLastRun(d){lastRun=(d&&d.actions&&d.actions.length&&(d.path==='FAST'||d.path==='EXPAND'||d.path==='PARTIAL'))?d:null;
 const b=$('btnDispatch');if(b){b.disabled=!lastRun;
 b.title=lastRun?('把最近一次运行的 '+lastRun.actions.length+' 条指令下发到 MQTT 分拣线'):'先运行并产生可执行指令 (FAST/EXPAND/PARTIAL) 才能下发'}}
async function dispatchRun(){if(!lastRun)return;const keep=lastRun;
 const b=$('btnDispatch');b.disabled=true;
 try{const r=await jpost('/dispatch',{actions:keep.actions,path:keep.path});
  lastRun=null;setLastRun(null);
  $('out').innerHTML=sec('📤 分拣下发')+`<pre class="path-FAST" style="font-size:14px">✅ 已入队 job=${r.job_id} (排队 ${r.queued}) — 结果见网关 :8123/stats 与 sort/event/batch</pre>`+$('out').innerHTML;
 }catch(e){lastRun=keep;setLastRun(keep);
  $('out').innerHTML=sec('📤 分拣下发')+`<pre style="color:#f87171">❌ ${e}</pre>`+$('out').innerHTML;}}
function b64(buf){const u=new Uint8Array(buf);let s='';for(let i=0;i<u.length;i+=8192)s+=String.fromCharCode.apply(null,u.subarray(i,i+8192));return btoa(s)}
async function render(d){
 drawFlow(d);
 setLastRun(d);
 refreshStock(); // 常驻库存条: 每次运行后都刷新 (分拣自动记账 / 出库 / 清空 都保持同步)
 // 台账面板开着时, 每次产生可执行下发 (已自动记入) 就刷新
 if(ledOpen&&d.actions&&d.actions.length&&['FAST','EXPAND','PARTIAL'].includes(d.path))refreshLedger();
 const pcls='path-'+d.path;
 const MATCN={red_apple:'红苹果',green_apple:'绿苹果',banana:'香蕉',pear:'梨',
  red_block:'红色方块',green_block:'绿色方块',yellow_block:'黄色方块',blue_block:'蓝色方块'};
 $('cards').innerHTML=card('路径',d.path,pcls)+card('E2E',d.e2e_ms+' ms')+card('ASR',d.asr_ms+' ms')
   +card('Laya',d.laya_ms+' ms')+(d.dist_ms?card('LLM 分配',d.dist_ms+' ms'):d.bypass_ms?card('LLM 旁路',d.bypass_ms+' ms'):card('LLM 旁路','—','muted'));
 let out='';
 if(d.domain==='yard'){
  const lines=d.actions.map(a=>`${a.yard}号仓库: ${MATCN[a.material]||a.material} × ${a.qty}`);
  let yc=''; [1,2,3].forEach(y=>{
   const items=d.actions.filter(a=>a.yard===y);
   const tot=items.reduce((s,a)=>s+a.qty,0);
   const detail=items.length?items.map(a=>
    `<div class="yd">${MATCN[a.material]||a.material} × ${a.qty}</div>`).join('')
    :'<div class="yd">—</div>';
   yc+=`<div class="card"><div class="k">${y}号仓库</div><div class="v ${tot?'':'muted'}">${tot?tot+' 件':'空'}</div>${detail}</div>`;
  });
  $('yardcards').innerHTML=yc;
  out+=sec('🚚 仓库分拣下发 <span class="chip">'+((d.path==='FAST'||d.path==='EXPAND'||d.path==='PARTIAL')?'PLC 可执行':d.path)+'</span>')
   +(lines.length?`<pre>${lines.join('<br>')}</pre>`:'<pre class="muted">无下发指令 (暂停/空订单)</pre>')
   +(d.dist_total?`<div class="yd" style="margin-top:6px">⚙ 分配指令 · 总量 ${d.dist_total} 件 · 已对账</div>`:'');
 } else $('yardcards').innerHTML='';
 const badge=d.path==='FAST'?'✅ FAST 快路径':(d.path==='QA'?(d.qa_source==='llm'?'💬 简单回答 · LLM 兜底':'💬 简单回答'):(d.path==='PARTIAL'?'⚠️ PARTIAL 部分下发 · 超纲挂起':(d.path==='EXPAND'?'⚙️ EXPAND LLM 分配展开':(d.path==='BYPASS'?'⚠️ LLM 旁路':'🛑 暂停 + 人工确认'))));
 out+=sec('最终下发')+`<pre class="${pcls}" style="font-size:15px">${badge}${String.fromCharCode(10)}${d.answer!=null?d.answer+(d.qa_source==='llm'?' (≤30字 · LLM 兜底, 仅转述注入事实, 未下发 PLC)':' (≤30字 · 规则层直答, 未下发 PLC)'):JSON.stringify(d.actions)}</pre>`;
 $('out').innerHTML=out;
 let h='';
 h+=sec('ASR / 输入 <span class="chip">'+(d.asr_model||'')+'</span>')+`<pre>${d.asr_text}</pre>`+(d.last_wav?`<a style="color:#60a5fa;font-size:12px" href="/audio/file?file=${d.last_wav}" target="_blank">🔊 听一下 ASR 实际收到的音频</a>`:'');
 if(d.answer!=null)h+=sec('问答 '+(d.qa_source==='llm'?'(LLM 兜底 · 仅转述注入事实)':'(规则层直答)'))+`<pre>${d.answer}</pre>`;
 h+=sec('规则层')+`<pre>归一化: ${d.normalized}${String.fromCharCode(10)}否定项: ${d.negated.join(', ')||'无'}${String.fromCharCode(10)}提及: ${JSON.stringify(d.mentions)}</pre>`;
 if(Object.keys(d.laya_answers).length){
  h+=sec('Laya 决策 (每提及单独请求)')+'<pre>'+Object.entries(d.laya_answers).map(([k,v])=>
   `${k}: choice=${v.choice} conf=${v.confidence}  p=${JSON.stringify(v.probabilities)}`).join('<br>')+'</pre>';
 }
 h+=sec('校验器')+`<pre>通过: ${d.validator_ok?'✅':'❌'}  动作: ${JSON.stringify(d.actions)}  拒绝原因: ${d.reject_reasons.join('; ')||'无'}</pre>`;
 if(d.bypass)h+=sec('LLM 旁路')+`<pre>输出提及: ${JSON.stringify(d.bypass.raw_mentions)}  原因: ${d.bypass.reasons.join('; ')||'无'}</pre>`;
 $('tracebody').innerHTML=h;
 $('trace').open=d.path!=='FAST';
}
let ac=null,proc=null,src=null,stream=null,armed=false,flushing=false;
let bufs=[],speechStart=0,speechUntil=0,ctxRate=48000;
let floorRms=0.001,floorN=0; // 噪声底自适应
let wakeOn=false;                     // 唤醒词模式 (可选开关, 默认关)
let recActive=false,recBufs=[],pttHeld=false; // 按住说话 / 录音识别
let wmode='off',cmdStart=0,tickTimer=0; // idle=等唤醒词, cmd=听指令, off=关
const SILENCE=1200,MINLEN=1200,MAXLEN=30000; // 指令模式
const W_SILENCE=600,W_MINLEN=350,W_MAXLEN=4000; // 唤醒模式 (短窗口, 只看最近 3s)
const WAKE_RE=/优优|悠悠|由由|柚柚|优由|youyou|you you/i;
function thr(){ return Math.max(0.0015, floorRms*3.5) } // 动态阈值
function refreshState(){
 const s=$('micstate');
 if(!armed){ s.textContent='未授权：先点「🎤 授权麦克风」'; }
 else if(flushing){ s.textContent='识别中…'; }
 else if(recActive){ s.textContent='🔴 录音中… 松开(空格/手指)发送'; }
 else if(wakeOn&&wmode==='cmd'){ s.textContent='🔔 已唤醒, 请说指令… (30秒内有效)'; }
 else if(wakeOn){ s.textContent='唤醒中… 说「优优」唤醒, 然后说指令'; }
 else { s.textContent='就绪：按住「🗣 按住说话」或 空格键 录音 (采样率 '+ctxRate+'Hz)'; }
}
function authToggle(){ armed?stopListen():startListen() }
async function startListen(){
 try{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:true}})}
 catch(e){$('micstate').textContent='麦克风不可用: '+e.message;return}
 try{ac=new AudioContext({sampleRate:16000})}catch(e){ac=new AudioContext()}
 ctxRate=ac.sampleRate;
 src=ac.createMediaStreamSource(stream);
 proc=ac.createScriptProcessor(2048,1,1);
 proc.onaudioprocess=function(e){ onChunk(e.inputBuffer) };
 src.connect(proc); proc.connect(ac.destination);
 armed=true; speechStart=0; speechUntil=0; bufs=[]; recBufs=[]; floorRms=0.001; floorN=0;
 wmode=wakeOn?'idle':'off';
 if(wakeOn&&!tickTimer)tickTimer=setInterval(wakeTick,300);
 $('micbtn').textContent='⏹ 撤销授权';
 $('micbtn').classList.remove('primary');
 $('ptt').disabled=false; $('wakesw').disabled=false;
 refreshState();
}
function stopListen(){
 armed=false;
 if(tickTimer){clearInterval(tickTimer);tickTimer=0}
 recActive=false; recBufs=[]; pttHeld=false;
 try{proc.disconnect();src.disconnect()}catch(e){}
 try{ac.close()}catch(e){}
 if(stream)stream.getTracks().forEach(t=>t.stop());
 proc=src=stream=ac=null;
 $('micbtn').textContent='🎤 授权麦克风';
 $('micbtn').classList.add('primary');
 $('ptt').disabled=true; $('wakesw').disabled=true; $('wakesw').checked=false; wakeOn=false;
 $('lv').style.width='0';
 refreshState();
}
function wakeToggle(){ // 唤醒词模式开关
 if(!armed)return;
 wakeOn=$('wakesw').checked;
 if(wakeOn){ wmode='idle'; speechStart=0; speechUntil=0; bufs=[];
  if(!tickTimer)tickTimer=setInterval(wakeTick,300);
 } else { if(tickTimer){clearInterval(tickTimer);tickTimer=0} wmode='off'; bufs=[]; }
 refreshState();
}
function pttDown(e){ // 按住说话: 按下开始录音 (鼠标/触屏/空格; 空格路径不带事件对象)
 if(e)e.preventDefault();
 if(!armed||flushing||recActive)return;
 pttHeld=true; recActive=true; recBufs=[];
 refreshState();
}
function pttUp(){ // 松开 -> 识别
 if(!pttHeld)return;
 pttHeld=false;
 stopRec();
}
// 空格键 = 按住说话: keydown 开始录音, keyup 发送识别
document.addEventListener('keydown',e=>{
 if(e.code!=='Space'||e.repeat)return;
 const a=document.activeElement;
 if(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.isContentEditable))return; // 打字中不拦截
 if(a&&a.tagName==='SELECT')return; // 下拉框空格展开, 保持默认
 if(a&&a.tagName==='BUTTON')e.preventDefault(); // 焦点在按钮上时, 空格不得激活按钮
 if(!armed||flushing||recActive)return;
 e.preventDefault();
 pttDown();
});
document.addEventListener('keyup',e=>{
 if(e.code!=='Space')return;
 const a=document.activeElement;
 if(a&&a.tagName==='BUTTON')e.preventDefault(); // 取消按下 keydown 残留的按钮激活
 if(pttHeld){e.preventDefault();pttUp();}
});
function stopRec(){
 if(!recActive)return;
 recActive=false;
 const c=recBufs; recBufs=[];
 if(c.length<10){ // <~1.3s 太短, 不识别
  refreshState();
  $('micstate').textContent='录音太短，请重试'; setTimeout(refreshState,1600);
  return;
 }
 flushWav(c,false);
}
function wakeTick(){ // 指令模式 30s 无话自动回到等唤醒
 if(!armed||!wakeOn||wmode!=='cmd'||flushing)return;
 if(performance.now()-cmdStart>30000 && performance.now()>speechUntil+2000) setModeIdle();
}
function setModeIdle(){ wmode='idle'; speechStart=0; speechUntil=0; refreshState(); }
function setModeCmd(){ wmode='cmd'; cmdStart=performance.now(); speechStart=0; speechUntil=0; bufs=[]; refreshState(); }
function onChunk(ib){
 if(!armed||!ac)return; // 识别中不进唤醒缓冲, 避免下一句混进上一句
 const L=ib.getChannelData(0);
 const R=ib.numberOfChannels>1?ib.getChannelData(1):null;
 const n=L.length, mono=new Float32Array(n);
 for(let i=0;i<n;i++) mono[i]=R?(L[i]+R[i])/2:L[i];
 const dec=to16k(mono,ctxRate);
 let s=0; for(let i=0;i<dec.length;i++) s+=dec[i]*dec[i];
 const rms=dec.length?Math.sqrt(s/dec.length):0;
 $('lv').style.width=Math.min(100,rms*3500)+'%';
 const now=performance.now();
 if(floorN<200||rms<floorRms){ if(rms<floorRms*2.5){ floorRms=(floorRms*floorN+rms)/(floorN+1); floorN++; } }
 if(recActive){ // 按住说话/录音识别: 直接累积 (上限 30s)
  if(!flushing){ recBufs.push(dec); if(recBufs.length>2344)recBufs.shift(); }
  return;
 }
 if(!wakeOn||flushing)return; // 唤醒词关着且没在录音: 不处理
 if(rms>thr())speechUntil=now+SILENCE;
 if(speechStart===0&&rms>thr())speechStart=now;
 bufs.push(dec);
 if(wmode==='idle'&&bufs.length>24)bufs.shift(); // 唤醒模式只保留最近 ~3s
 else if(bufs.length>3000)bufs.shift();
 if(!speechStart)return;
 if(wmode==='idle'){
  if((now>speechUntil+(W_SILENCE-SILENCE)&&now-speechStart>W_MINLEN)||now-speechStart>W_MAXLEN) flushUtterance(true);
 } else if(wmode==='cmd'){
  if((now>speechUntil&&now-speechStart>MINLEN)||now-speechStart>MAXLEN) flushUtterance(false);
 }
}
function to16k(mono,sr){
 if(sr===16000)return mono;
 const r=sr/16000,out=new Float32Array(Math.floor(mono.length/r));
 for(let i=0;i<out.length;i++){let s=0,c=0,j=i*r;
  for(;j<(i+1)*r&&j<mono.length;j+=1){s+=mono[j];c++} out[i]=c?s/c:0}
 const lp=new Float32Array(out.length); // 轻低通, 压一下盒式抽取的混叠
 for(let i=0;i<lp.length;i++){
  const a=i>0?out[i-1]:out[0], b=out[i], c=i<out.length-1?out[i+1]:out[out.length-1];
  lp[i]=(a+2*b+c)*0.25;
 }
 return lp;
}
function flushUtterance(isWake, tailN){ // 唤醒词/指令窗口 (来自 bufs)
 const chunks=tailN?bufs.slice(-tailN):bufs;
 bufs=[]; // 不保留缓冲, 重新开始累计
 speechStart=0;
 flushWav(chunks, isWake);
}
function flushWav(chunks, isWake){
 flushing=true;
 refreshState();
 const total=chunks.reduce((a,b)=>a+b.length,0), data=new Float32Array(total); let off=0;
 for(const b of chunks){data.set(b,off);off+=b.length}
 if(!total){flushing=false;refreshState();return}
 let peak=0; for(let i=0;i<data.length;i++){const a=Math.abs(data[i]); if(a>peak)peak=a}
 let g=peak>0.001?0.9/peak:1; if(g>4)g=4; if(g<0.3)g=0.3; // 峰值归一, 轻声也能被听清
 for(let i=0;i<data.length;i++) data[i]*=g;
 const n=data.length,buf=new ArrayBuffer(44+n*2),v=new DataView(buf);
 const ws=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
 ws(0,'RIFF');v.setUint32(4,36+n*2,true);ws(8,'WAVE');ws(12,'fmt ');
 v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
 v.setUint32(24,16000,true);v.setUint32(28,32000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);
 ws(36,'data');v.setUint32(40,n*2,true);
 for(let i=0;i<n;i++){const x=Math.max(-1,Math.min(1,data[i]));v.setInt16(44+i*2,x<0?x*32768:x*32767,true)}
 const fr=new FileReader();
 fr.onload=async()=>{
  if(isWake){
   try{
    const d=await jpost('/run',{wav:fr.result.split(',')[1],wake:true});
    if(armed&&d.asr_text&&WAKE_RE.test(d.asr_text)){
     const rest=(d.asr_text.replace(WAKE_RE,'').replace(/[，。,.]/g,'')).trim();
     if(rest.length>=2){
     // 唤醒词后紧跟了指令 ("优优给我两个苹果"), 直接跑管线, 不用重新说
     $('out').innerHTML=sec('ASR 识别中…');
     try{render(await jpost('/run',{text:rest,domain:domain,qa_llm:$('qallm').checked}))}catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
     setModeIdle();
     } else setModeCmd(); // 只说了唤醒词, 等指令
    }
    else if(armed) setModeIdle();
   }catch(e){ if(armed) setModeIdle() }
   flushing=false; refreshState(); return;
  }
  // 两段式: 先 /asr (识别文字 ~1-2s 即时显示) → 再 /run 复用结果 (不重复 ASR);
  // /asr 失败时回落 单发 /run(wav) (老行为, 全程识别完才显示)
  $('out').innerHTML=sec('ASR 识别中…');
  const b64=fr.result.split(',')[1];
  let asrInfo=null;
  try{
   asrInfo=await jpost('/asr',{wav:b64});
   $('out').innerHTML=sec('ASR 识别完成 <span class="chip">'+(asrInfo.asr_ms||0)+' ms</span>')
    +`<pre>${asrInfo.text||'(空)'}</pre>`+'<div class="yd muted">分拣管线运行中…</div>';
  }catch(e){ /* 回落单发 */ }
  try{
   if(asrInfo)render(await jpost('/run',{text:asrInfo.text,domain:domain,qa_llm:$('qallm').checked,
    asr_ms:asrInfo.asr_ms,last_wav:asrInfo.last_wav}));
   else render(await jpost('/run',{wav:b64,domain:domain,qa_llm:$('qallm').checked}));
  }catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
  if(armed){speechStart=0;flushing=false; if(wakeOn)setModeIdle()}
  else flushing=false;
  refreshState();
 };
 fr.readAsDataURL(new Blob([buf],{type:'audio/wav'}));
}
let PREDS=[];
function runPresetSel(){
 const sel=$('preset'), i=+sel.value, p=PREDS[i];
 sel.selectedIndex=0;
 if(!p)return;
 $('txt').value=p.text; runText();
}
fetch('/presets').then(r=>r.json()).then(ps=>{PREDS=ps.filter(p=>p.domain==='yard');
 $('preset').innerHTML='<option value="" selected>预设指令…</option>'+PREDS.map((p,i)=>
  `<option value="${i}">${p.name}</option>`).join('');
});
$('txt').addEventListener('keydown',e=>{if(e.key==='Enter')runText()});
// 「LLM 兜底」开关: 状态存 localStorage (本浏览器记忆), 随每次 /run 请求发给后端
(function(){const s=$('qallm');if(!s)return;
 try{
  s.checked=(localStorage.getItem('qa_llm')==='1');
  s.addEventListener('change',()=>localStorage.setItem('qa_llm',s.checked?'1':'0'));
 }catch(e){ /* localStorage 不可用 (如隐私模式): 不记忆, 开关仍可用 */ }
})();
refreshState();
refreshStock(); // 常驻库存条: 页面加载即显示 (不用点台账面板)
</script></div></body></html>"""

# 光门安装示意图嵌入 (edge101/docs/gate_layout.svg 独立资产, 改图不用动本页代码)
PAGE = PAGE.replace("@GATE_SVG@", GATE_SVG)



@app.get("/audio")
def audio_list():
    import glob
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(AUDIO_DIR, "tts_*.wav")))


@app.get("/audio/file")
def audio_file(file: str):
    from fastapi.responses import FileResponse
    p = os.path.join(AUDIO_DIR, os.path.basename(file))
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav")


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    import uvicorn
    print("[loop_ui] 预热 ASR 常驻模型…", flush=True)
    bi.asr_warmup()
    print("[loop_ui] 就绪", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8002")), log_level="info")
