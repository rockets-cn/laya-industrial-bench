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
import urllib.request

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

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

import bench_pipeline  # noqa: E402  (already imported by bench_industrial)
bench_pipeline.MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b")

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
    ("料场 双场双料", "给1号料场送两个红苹果，3号料场送10个蓝色方块", "yard"),
    ("料场 倒序", "两个红苹果送到3号料场，1号料场来五个香蕉", "yard"),
    ("料场 否定", "1号料场送四个梨，不要绿色方块", "yard"),
    ("料场 超纲 4号", "给4号料场送两个苹果", "yard"),
    ("料场 超纲 缺场", "送两个红苹果", "yard"),
    ("料场 密集无标点", "一号仓库一个香蕉两个苹果二号仓库三个苹果四个香蕉三号仓库一个梨一个香蕉", "yard"),
]

SIDES = {"fruit": bi, "block": bb, "yard": by}


def run_side(side, text):
    """单领域跑一圈: 规则层 -> Laya -> 校验器 -> (LLM旁路). side: fruit|block|yard."""
    ns = SIDES[side]
    norm, mentions, neg = ns.normalize(text)
    if not mentions and not neg:
        return {"side": side, "empty": True, "normalized": norm, "negated": neg,
                "mentions": [], "laya_ms": 0.0, "answers": {}, "ok": True,
                "actions": [], "reasons": [], "path": "EMPTY", "bypass_ms": 0.0,
                "bypass": None}
    dt_l, answers = ns.laya_decide(mentions) if mentions else (0.0, {})
    ok, actions, reasons = ns.validate(mentions, answers)
    path, dt_b, bypass = "FAST", 0.0, None
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
    return {"side": side, "empty": False, "normalized": norm, "negated": neg,
            "mentions": mentions, "laya_ms": dt_l, "answers": answers, "ok": ok,
            "actions": actions, "reasons": reasons, "path": path,
            "bypass_ms": dt_b, "bypass": bypass}


def run_pipeline(text, asr_dt=0.0, asr_text=None, last_wav=None, domain="fruit"):
    """Text/normalized -> (规则层 -> Laya -> 校验器 -> LLM旁路). Returns full trace dict.
    domain: fruit | block | mixed (两个规则层并行, 各抽各的, 动作合并)."""
    t0 = time.perf_counter()
    sides = ["fruit", "block"] if domain == "mixed" else [domain]
    results = [run_side(s, text) for s in sides]
    active = [r for r in results if not r["empty"]]
    if not active:
        results = [{"side": sides[0], "empty": False, "normalized": text, "negated": [],
                    "mentions": [], "laya_ms": 0.0, "answers": {}, "ok": False,
                    "actions": [], "reasons": ["空订单"], "path": "PAUSE+HUMAN",
                    "bypass_ms": 0.0, "bypass": None}]
        active = results
    order = {"FAST": 0, "BYPASS": 1, "PAUSE+HUMAN": 2}
    path = max((r["path"] for r in active), key=lambda p: order[p])
    merged_answers = {}
    pfx = {"fruit": "f_", "block": "b_", "yard": "y_"}
    for r in results:
        for k, v in r["answers"].items():
            merged_answers[pfx[r["side"]] + k] = v
    merged_bypass = {k: v for r in results for k, v in
                     (r["bypass"].items() if r["bypass"] else [])} or None
    return {
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
        "e2e_ms": round((time.perf_counter() - t0) * 1000),
    }


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    wav = body.get("wav")
    mic = body.get("mic")
    wake = bool(body.get("wake"))
    domain = body.get("domain") or "fruit"
    if domain not in ("fruit", "block", "mixed", "yard"):
        domain = "fruit"
    asr_dt, asr_text = 0.0, None
    last_wav = None
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
    elif not text:
        raise HTTPException(422, "text, wav or mic required")
    if wake:
        # 唤醒词检测: 只做 ASR, 不跑 Laya/LLM 管线
        return {"wake": True, "asr_text": text, "asr_ms": round(asr_dt * 1000),
                "asr_model": getattr(bi, "LAST_ASR_MODEL", "?")}
    return run_pipeline(text, asr_dt, None, last_wav, domain=domain)


@app.get("/presets")
def presets():
    return [{"name": n, "text": t, "domain": d} for n, t, d in PRESETS]


PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Laya 料场分拣回路 v9</title>
<style>
 body{font-family:system-ui,'Noto Sans CJK SC',sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 .wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px}
 h1{font-size:18px;font-weight:600;margin:0}
 .ver{color:#6b7280;font-size:12px;font-weight:400;margin-left:8px}
 .cards{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
 .card{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:10px 14px;flex:1;min-width:110px}
 .card .k{font-size:11px;color:#8a91a0} .card .v{font-size:19px;font-weight:700;margin-top:3px}
 .path-FAST{color:#4ade80}.path-BYPASS{color:#facc15}.path-PAUSE+HUMAN{color:#f87171}
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
<h1>🏭 Laya 工业决策回路 · 料场分拣<span class="ver">v9 · 实时路径不含 LLM · LLM 仅旁路 · 失败降级 暂停+人工</span></h1>
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
 <g id="fn6" class="n off"><rect x="662" y="14" width="104" height="36" rx="9"/><text x="714" y="29" text-anchor="middle">LLM 旁路</text><text x="714" y="43" text-anchor="middle" class="sub">仅校验拒绝时</text><text id="fn-bypass" x="714" y="66" text-anchor="middle" class="ms">未触发</text></g>
 <g id="fn7" class="n off"><rect x="794" y="14" width="104" height="36" rx="9"/><text x="846" y="29" text-anchor="middle">PLC 下发</text><text x="846" y="43" text-anchor="middle" class="sub">暂停 = 不发</text><text id="fn-e2e" x="846" y="66" text-anchor="middle" class="ms">—</text></g>
</svg>
<div class="row">
 <select id="preset" onchange="runPresetSel()" title="测试指令预设"><option value="" selected>预设指令…</option></select>
 <input id="txt">
 <button class="primary" onclick="runText()">▶ 运行</button>
</div>
<div class="row">
 <button id="micbtn" class="primary" style="min-width:120px" onclick="authToggle()">🎤 授权麦克风</button>
 <button id="ptt" title="按住说话 (或按住 空格键)" style="min-width:120px;touch-action:none;user-select:none" onpointerdown="pttDown(event)" onpointerup="pttUp()" onpointerleave="pttUp()" onpointercancel="pttUp()" oncontextmenu="event.preventDefault();return false" disabled>🗣 按住说话</button>
 <label style="display:flex;align-items:center;gap:5px;font-size:13px;color:#8a91a0;cursor:pointer"><input type="checkbox" id="wakesw" onchange="wakeToggle()" disabled> 唤醒词「优优」</label>
 <div style="flex:1;min-width:160px"><div id="micstate" class="muted"></div><div class="bar"><i id="lv"></i></div></div>
</div>
<div id="cards" class="cards"></div>
<div id="yardcards" class="cards"></div>
<div id="out"></div>
<details class="fold" id="trace"><summary>全链路 trace (ASR 原文 · 规则层 · Laya 概率 · 校验器 · 旁路)</summary><div id="tracebody"></div></details>
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
const domain='yard'; // 页面专属料场分拣 (后端 API 仍支持 fruit/block/mixed 基准域)
function drawFlow(d){
 const set=(id,c)=>{const el=$(id);el.classList.remove('on','off','warn','stop');el.classList.add(c)};
 if(d.path==='FAST'){['fn1','fn2','fn3','fn4','fn5','fn7'].forEach(i=>set(i,'on'));set('fn6','off')}
 else if(d.path==='BYPASS'){['fn1','fn2','fn3','fn4','fn5'].forEach(i=>set(i,'on'));set('fn6','warn');set('fn7','warn')}
 else{['fn1','fn2','fn3','fn4','fn5'].forEach(i=>set(i,'on'));set('fn6',d.bypass_ms?'warn':'off');set('fn7','stop')}
 $('fn-asr').textContent=d.asr_ms!=null?d.asr_ms+' ms':'—';
 $('fn-laya').textContent=d.laya_ms!=null?d.laya_ms+' ms':'—';
 $('fn-bypass').textContent=d.bypass_ms?d.bypass_ms+' ms':'未触发';
 $('fn-e2e').textContent='E2E '+d.e2e_ms+' ms';
}
async function runText(){
 const t=$('txt').value.trim(); if(!t)return;
 $('out').innerHTML=sec('运行中…');
 try{render(await jpost('/run',{text:t,domain:domain}))}catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
}
function b64(buf){const u=new Uint8Array(buf);let s='';for(let i=0;i<u.length;i+=8192)s+=String.fromCharCode.apply(null,u.subarray(i,i+8192));return btoa(s)}
function render(d){
 drawFlow(d);
 const pcls='path-'+d.path;
 const MATCN={red_apple:'红苹果',green_apple:'绿苹果',banana:'香蕉',pear:'梨',
  red_block:'红色方块',green_block:'绿色方块',yellow_block:'黄色方块',blue_block:'蓝色方块'};
 $('cards').innerHTML=card('路径',d.path,pcls)+card('E2E',d.e2e_ms+' ms')+card('ASR',d.asr_ms+' ms')
  +card('Laya',d.laya_ms+' ms')+(d.bypass_ms?card('LLM 旁路',d.bypass_ms+' ms'):card('LLM 旁路','—','muted'));
 let out='';
 if(d.domain==='yard'){
  const lines=d.actions.map(a=>`${a.yard}号料场: ${MATCN[a.material]||a.material} × ${a.qty}`);
  let yc=''; [1,2,3].forEach(y=>{
   const items=d.actions.filter(a=>a.yard===y);
   const tot=items.reduce((s,a)=>s+a.qty,0);
   const detail=items.length?items.map(a=>
    `<div class="yd">${MATCN[a.material]||a.material} × ${a.qty}</div>`).join('')
    :'<div class="yd">—</div>';
   yc+=`<div class="card"><div class="k">${y}号料场</div><div class="v ${tot?'':'muted'}">${tot?tot+' 件':'空'}</div>${detail}</div>`;
  });
  $('yardcards').innerHTML=yc;
  out+=sec('🚚 料场分拣下发 <span class="chip">'+(d.path==='FAST'?'PLC 可执行':d.path)+'</span>')
   +(lines.length?`<pre>${lines.join('<br>')}</pre>`:'<pre class="muted">无下发指令 (暂停/空订单)</pre>');
 } else $('yardcards').innerHTML='';
 const badge=d.path==='FAST'?'✅ FAST 快路径':(d.path==='BYPASS'?'⚠️ LLM 旁路':'🛑 暂停 + 人工确认');
 out+=sec('最终下发')+`<pre class="${pcls}" style="font-size:15px">${badge}${String.fromCharCode(10)}${JSON.stringify(d.actions)}</pre>`;
 $('out').innerHTML=out;
 let h='';
 h+=sec('ASR / 输入 <span class="chip">'+(d.asr_model||'')+'</span>')+`<pre>${d.asr_text}</pre>`+(d.last_wav?`<a style="color:#60a5fa;font-size:12px" href="/audio/file?file=${d.last_wav}" target="_blank">🔊 听一下 ASR 实际收到的音频</a>`:'');
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
     try{render(await jpost('/run',{text:rest,domain:domain}))}catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
     setModeIdle();
     } else setModeCmd(); // 只说了唤醒词, 等指令
    }
    else if(armed) setModeIdle();
   }catch(e){ if(armed) setModeIdle() }
   flushing=false; refreshState(); return;
  }
  $('out').innerHTML=sec('ASR 识别中…');
  try{render(await jpost('/run',{wav:fr.result.split(',')[1],domain:domain}))}
  catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
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
refreshState();
</script></div></body></html>"""



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
