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
]


def run_side(side, text):
    """单领域跑一圈: 规则层 -> Laya -> 校验器 -> (LLM旁路). side: fruit|block."""
    ns = bi if side == "fruit" else bb
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
    for r in results:
        for k, v in r["answers"].items():
            merged_answers[("f_" if r["side"] == "fruit" else "b_") + k] = v
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
    if domain not in ("fruit", "block", "mixed"):
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
<title>Laya 工业决策回路</title>
<style>
 body{font-family:system-ui,'Noto Sans CJK SC',sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 .wrap{max-width:960px;margin:0 auto;padding:24px 16px 60px}
 h1{font-size:20px;font-weight:600} .sub{color:#8a91a0;font-size:13px;margin-bottom:20px}
 .cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
 .card{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:14px;flex:1;min-width:180px}
 .card .k{font-size:11px;color:#8a91a0} .card .v{font-size:22px;font-weight:700;margin-top:4px}
 .path-FAST{color:#4ade80}.path-BYPASS{color:#facc15}.path-PAUSE+HUMAN{color:#f87171}
 textarea{width:100%;box-sizing:border-box;background:#171a21;color:#e6e6e6;border:1px solid #262b36;
  border-radius:10px;padding:12px;font-size:15px;min-height:70px;resize:vertical}
 .row{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
 button{background:#1e2430;color:#e6e6e6;border:1px solid #2c3342;border-radius:8px;padding:7px 12px;
  cursor:pointer;font-size:13px} button:hover{background:#262d3d}
 button.primary{background:#2563eb;border-color:#2563eb;color:#fff;font-weight:600}
 .drop{border:1px dashed #3a4152;border-radius:10px;padding:14px;color:#8a91a0;font-size:13px;cursor:pointer}
 .drop.on{border-color:#4ade80;color:#4ade80}
 pre{background:#12141a;border:1px solid #262b36;border-radius:10px;padding:12px;font-size:12.5px;
  overflow:auto;margin:8px 0;white-space:pre-wrap}
 .sec{font-size:12px;color:#8a91a0;margin:14px 0 4px;text-transform:uppercase;letter-spacing:.05em}
 .pill{display:inline-block;background:#1e2430;border-radius:20px;padding:3px 10px;font-size:12px;margin:2px 4px 2px 0}
 .muted{color:#6b7280;font-size:12px}
 .bar{height:6px;background:#262b36;border-radius:3px;overflow:hidden;margin-top:6px}
 .bar i{display:block;height:100%;width:0;background:#4ade80;transition:width .08s}
 .chip{display:inline-block;background:#1e2430;border-radius:6px;padding:2px 8px;font-size:11px;color:#8a91a0;margin-left:6px}
</style></head><body><div class="wrap">
<h1>🏭 Laya 工业决策回路 <span class="muted">语音/文本 → ASR(SenseVoice) → 规则归一化 → Laya → 校验器 → (LLM旁路)</span></h1>
<div class="sub">实时路径不含 LLM；LLM 仅校验拒绝时旁路；任何失败安全降级为 暂停+人工。</div>
<div class="row">
 <input id="txt" style="flex:1;min-width:260px;background:#171a21;color:#e6e6e6;border:1px solid #262b36;border-radius:8px;padding:10px 12px;font-size:14px">
 <button class="primary" onclick="runText()">▶ 运行</button>
</div>
<div class="row" id="domainrow">
 <button class="primary" data-dom="fruit" onclick="setDomain('fruit')">🍎 水果回路</button>
 <button data-dom="block" onclick="setDomain('block')">🧊 彩色方块回路</button>
 <button data-dom="mixed" onclick="setDomain('mixed')">🍎+🧊 混合</button>
 <span id="domlabel" class="muted" style="align-self:center"></span>
</div>
<div class="row" id="presets"></div>
<div class="row">
 <button id="micbtn" class="primary" style="min-width:230px" onclick="micToggle()">🎤 点一下授权，唤醒词「优优」</button>
 <button id="mflush" style="min-width:110px" onclick="manualFlush()" disabled>⚡ 手动识别</button>
 <div style="flex:1;min-width:180px"><div id="micstate" class="muted"></div><div class="bar"><i id="lv"></i></div></div>
</div>
<div class="row">
 <label class="drop" id="drop">🎙 点击选择 WAV (16k mono) 或用下方测试音频<br><span class="muted">选择后自动识别 → 跑全链路</span>
 <input type="file" id="wavf" accept=".wav,audio/*" hidden></label>
 <span id="wavnames"></span>
</div>
<div id="cards" class="cards"></div>
<div id="out"></div>
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
function pills(arr){return arr.length?arr.map(x=>`<span class="pill">${x}</span>`).join(''):'<span class="muted">无</span>'}
let domain='fruit';
function setDomain(d){ domain=d;
 document.querySelectorAll('#domainrow button').forEach(b=>b.classList.toggle('primary',b.dataset.dom===d));
 $('domlabel').textContent={fruit:'当前: 水果回路',block:'当前: 彩色方块回路 (红/绿/黄/蓝 × 数量)',mixed:'当前: 混合 (两个规则层并行, 各抽各的)'}[d];
}
setDomain('fruit');
async function runText(){
 const t=$('txt').value.trim(); if(!t)return;
 $('out').innerHTML=sec('运行中…');
 try{render(await jpost('/run',{text:t,domain:domain}))}catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
}
function b64(buf){const u=new Uint8Array(buf);let s='';for(let i=0;i<u.length;i+=8192)s+=String.fromCharCode.apply(null,u.subarray(i,i+8192));return btoa(s)}
async function runWav(name){
 const f=$('wavf').files[0]; if(f){
  const fr=new FileReader(); fr.onload=async()=>{
   const b64=fr.result.split(',')[1];
   $('out').innerHTML=sec('ASR 识别中…');
   try{render(await jpost('/run',{wav:b64,domain:domain}))}catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
  }; fr.readAsDataURL(f);
 } else if(name){
  const r=await fetch('/audio/file?file='+encodeURIComponent(name)); const buf=await r.arrayBuffer();
  const b64=b64(buf);
  $('out').innerHTML=sec('ASR 识别中…');
  try{render(await jpost('/run',{wav:b64,domain:domain}))}catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
 }
}
function render(d){
 const pcls='path-'+d.path;
 $('cards').innerHTML=card('领域',{fruit:'水果',block:'方块',mixed:'混合'}[d.domain]||d.domain,'')+card('路径',d.path,pcls)+card('E2E',d.e2e_ms+' ms')
  +card('ASR',d.asr_ms+' ms')+card('Laya',d.laya_ms+' ms')+(d.bypass_ms?card('LLM 旁路',d.bypass_ms+' ms'):card('LLM 旁路','未触发'));
 let h='';
 h+=sec('ASR / 输入 <span class="chip" style="text-transform:none;letter-spacing:0">'+(d.asr_model||'')+'</span>')+`<pre>${d.asr_text}</pre>`+(d.last_wav?`<a class="muted" style="color:#60a5fa" href="/audio/file?file=${d.last_wav}" target="_blank">🔊 听一下 ASR 实际收到的音频</a>`:'');
 h+=sec('规则层')+`<pre>归一化: ${d.normalized}\n否定项: ${d.negated.join(', ')||'无'}\n提及: ${JSON.stringify(d.mentions)}</pre>`;
 if(Object.keys(d.laya_answers).length){
  h+=sec('Laya 决策 (每提及单独请求)')+'<pre>'+Object.entries(d.laya_answers).map(([k,v])=>
   `${k}: choice=${v.choice} conf=${v.confidence}  p=${JSON.stringify(v.probabilities)}`).join('\\n')+'</pre>';
 }
 h+=sec('校验器')+`<pre>通过: ${d.validator_ok?'✅':'❌'}  动作: ${JSON.stringify(d.actions)}  拒绝原因: ${d.reject_reasons.join('; ')||'无'}</pre>`;
 if(d.bypass)h+=sec('LLM 旁路')+`<pre>输出提及: ${JSON.stringify(d.bypass.raw_mentions)}  原因: ${d.bypass.reasons.join('; ')||'无'}</pre>`;
 const badge=d.path==='FAST'?'✅ FAST 快路径':(d.path==='BYPASS'?'⚠️ LLM 旁路':'🛑 暂停 + 人工确认');
 h+=sec('最终下发')+`<pre class="${pcls}" style="font-size:15px">${badge}\n${JSON.stringify(d.actions)}</pre>`;
 $('out').innerHTML=h;
}
let ac=null,proc=null,src=null,stream=null,armed=false,flushing=false;
let bufs=[],speechStart=0,speechUntil=0,ctxRate=48000;
let floorRms=0.001,floorN=0; // 噪声底自适应
let wmode='idle',cmdStart=0,tickTimer=0; // idle=等唤醒词, cmd=已唤醒听指令
const SILENCE=1200,MINLEN=1200,MAXLEN=30000; // 指令模式
const W_SILENCE=600,W_MINLEN=350,W_MAXLEN=4000; // 唤醒模式 (短窗口, 只看最近 3s)
const WAKE_RE=/优优|悠悠|由由|柚柚|优由|youyou|you you/i;
function thr(){ return Math.max(0.0015, floorRms*3.5) } // 动态阈值
function micToggle(){ armed?stopListen():startListen() }
function manualFlush(){
 if(!armed||flushing)return;
 if(wmode==='cmd'){ if(bufs.length>20){ speechStart=speechStart||performance.now(); flushUtterance(false) } }
 else{ // 绕过唤醒词, 直接识别最近 ~5s
  const n=Math.min(bufs.length,39);
  if(n>10){ wmode='cmd'; flushUtterance(false,n) } }
}
function setModeIdle(){
 wmode='idle'; speechStart=0; speechUntil=0;
 $('micstate').textContent='唤醒中… 说「优优」唤醒, 然后说指令';
}
function setModeCmd(){
 wmode='cmd'; cmdStart=performance.now(); speechStart=0; speechUntil=0; bufs=[];
 $('micstate').textContent='🔔 已唤醒, 请说指令… (30秒内有效)';
}
async function startListen(){
 try{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:true}})}
 catch(e){$('micstate').textContent='麦克风不可用: '+e.message;return}
 try{ac=new AudioContext({sampleRate:16000})}catch(e){ac=new AudioContext()}
 ctxRate=ac.sampleRate;
 src=ac.createMediaStreamSource(stream);
 proc=ac.createScriptProcessor(2048,1,1);
 proc.onaudioprocess=function(e){ onChunk(e.inputBuffer) };
 src.connect(proc); proc.connect(ac.destination);
 armed=true; speechStart=0; speechUntil=0; bufs=[]; floorRms=0.001; floorN=0;
 wmode='idle';
 $('micbtn').textContent='⏹ 停止'; $('mflush').disabled=false;
 $('micstate').textContent='唤醒中… 说「优优」唤醒, 然后说指令 (采样率 '+ctxRate+'Hz)';
 tickTimer=setInterval(wakeTick,300); // 唤醒/超时巡检
}
function stopListen(){
 armed=false;
 if(tickTimer){clearInterval(tickTimer);tickTimer=0}
 try{proc.disconnect();src.disconnect()}catch(e){}
 try{ac.close()}catch(e){}
 if(stream)stream.getTracks().forEach(t=>t.stop());
 proc=src=stream=ac=null;
 $('micbtn').textContent='🎤 点一下授权，然后直接说话';
 $('mflush').disabled=true;
 $('micstate').textContent=''; $('lv').style.width='0';
}
function wakeTick(){ // 指令模式 30s 无话自动回到等唤醒
 if(!armed||wmode!=='cmd'||flushing)return;
 if(performance.now()-cmdStart>30000 && performance.now()>speechUntil+2000) setModeIdle();
}
function onChunk(ib){
 if(!armed||!ac||flushing)return; // 识别中不录音, 避免下一句混进上一句
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
 if(rms>thr())speechUntil=now+SILENCE;
 if(speechStart===0&&rms>thr())speechStart=now;
 bufs.push(dec);
 if(wmode==='idle'&&bufs.length>24)bufs.shift(); // 唤醒模式只保留最近 ~3s
 else if(bufs.length>3000)bufs.shift();
 if(flushing||!speechStart)return;
 if(wmode==='idle'){
  if((now>speechUntil+ (W_SILENCE-SILENCE)&&now-speechStart>W_MINLEN)||now-speechStart>W_MAXLEN) flushUtterance(true);
 } else {
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
function flushUtterance(isWake, tailN){
 flushing=true; speechStart=0;
 $('micstate').textContent='识别中…'; $('lv').style.width='0';
 const chunks=tailN?bufs.slice(-tailN):bufs;
 const total=chunks.reduce((a,b)=>a+b.length,0), data=new Float32Array(total); let off=0;
 for(const b of chunks){data.set(b,off);off+=b.length} bufs=[]; // 不保留缓冲, 重新开始累计
 if(!total){flushing=false;return}
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
   flushing=false; return;
  }
  $('out').innerHTML=sec('ASR 识别中…');
  try{render(await jpost('/run',{wav:fr.result.split(',')[1]}))}
  catch(e){$('out').innerHTML=`<pre style="color:#f87171">${e}</pre>`}
  if(armed){speechStart=0;flushing=false;setModeIdle()}
  else flushing=false;
 };
 fr.readAsDataURL(new Blob([buf],{type:'audio/wav'}));
}
fetch('/presets').then(r=>r.json()).then(ps=>{$('presets').innerHTML=ps.map((p,i)=>
 `<button onclick="setDomain(${JSON.stringify(p.domain)});document.getElementById('txt').value=${JSON.stringify(p.text)};runText()">${p.name}</button>`).join('')});
fetch('/audio').then(r=>r.json()).then(fs=>{$('wavnames').innerHTML=fs.map(f=>
 `<button style="margin:2px" onclick="runWav('${f}')">🎵 ${f}</button>`).join(' ')});
$('drop').onclick=()=>$('wavf').click();
$('wavf').onchange=e=>{if(e.target.files.length){$('drop').classList.add('on');
 $('drop').firstChild.textContent='🎙 '+e.target.files[0].name+' ';runWav()}};
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
