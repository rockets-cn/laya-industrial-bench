#!/usr/bin/env python3
"""bench_web: 相机页 + 测位按钮 (2026-11-04 位置标定协议)
GET  http://127.0.0.1:8126/          -> 页面 (MJPEG 画面 + 按钮)
GET  /api/state                       -> 串口状态
POST /api/action {"cmd":"pos1"|"pos2"|"pos3"|"home"|"ok"|"bad", "note":"..."}
  pos1/pos2/pos3: 步进把香蕉移到 1/2/3 号仓接触点 (s=120/170/195mm), 不推杆
  home:           回到黄标 s=0
  ok/bad:         记录位置判定到 sort_calibration.jsonl
"""
import os, sys, re, json, time, threading, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np

sys.path.insert(0, "/home/rockets/laya-industrial-bench")
sys.argv = ["beltctl.py", "1"]
import beltctl as B

PORT = 8126
SERIAL = "/dev/ttyACM0"
RECORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sort_calibration.jsonl")
S_TARGET = {1: 120.0, 2: 170.0, 3: 221.0}   # 各仓接触点 s (mm)
STEPS_PER_MM = 69.1

# ---- mm 坐标系 (2026-11-04 新机位锚点链) ----
A0 = np.array([598.4, 96.8])
LANE = np.array([-0.707, 0.707])
ANCH = [(0.0, 0.0), (120.0, 139.6), (170.0, 230.1), (220.0, 346.1), (305.0, 545.0)]

def s_of(p):
    d = float((np.array(p) - A0) @ LANE)
    for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
        if d <= d2:
            return s1 + (d - d1) * (s2 - s1) / (d2 - d1)
    return 305.0

def lat_mm(p):
    d = float((np.array(p) - A0) @ LANE)
    sc = 2.32
    for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
        if d <= d2:
            sc = (d2 - d1) / (s2 - s1)
    return float(np.linalg.norm(np.array(p) - (A0 + LANE * d))) / sc

# ---- 串口 ----
FD = os.open(SERIAL, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
time.sleep(3.0)

def serial_cmd(line, wait=3.0):
    os.write(FD, (line + "\n").encode())
    t0, out = time.time(), b""
    while time.time() - t0 < wait:
        try:
            c = os.read(FD, 4096)
            if c:
                out += c
                if line.encode()[:8] in out:
                    break
        except BlockingIOError:
            time.sleep(0.02)
    return out.decode(errors="replace")

def read_pos(n=3):
    """停带时读香蕉位置 (3 次 YOLO 中位数)"""
    ps = []
    for _ in range(n):
        p, f = B.banana()
        if p is not None:
            ps.append(p)
        time.sleep(0.5)
    if len(ps) < 2:
        return None
    return np.median(ps, axis=0)

def step_mm(mm, reverse=False):
    steps = int(round(abs(mm) * STEPS_PER_MM))
    sign = " r" if reverse else ""
    while steps > 0:
        n = min(steps, 20000)
        serial_cmd(f"stepper {n}{sign}", max(4.0, n / 3628.0 * 1.5 + 2.0))
        steps -= n

BUSY = threading.Lock()
LAST = {"bin": None, "msg": "就绪"}

def do_action(cmd, note=""):
    with BUSY:
        if cmd in ("pos1", "pos2", "pos3"):
            bin_n = int(cmd[3])
            p = read_pos()
            if p is None:
                LAST.update(bin=bin_n, msg="带面未见香蕉, 请先放香蕉到带面")
                return dict(LAST)
            s0 = s_of(p)
            target = S_TARGET[bin_n]
            ds = target - s0
            if abs(ds) > 2:
                step_mm(ds, reverse=(ds < 0))
            p2 = read_pos()
            s1 = s_of(p2) if p2 is not None else None
            # 到位验证: stepper 偶发不执行 → 残差 >8mm 时自动重试一次
            if s1 is not None and abs(s1 - target) > 8 and abs(ds) > 5:
                ds2 = target - s1
                step_mm(ds2, reverse=(ds2 < 0))
                p3 = read_pos()
                if p3 is not None:
                    s1, p2 = s_of(p3), p3
            lat = lat_mm(p2) if p2 is not None else None
            LAST.update(bin=bin_n, msg=f"仓{bin_n}: s {s0:.0f} -> {s1:.0f}mm (目标 {target:.0f}), 横向偏移 {lat:+.0f}mm")
            rec = dict(ts=time.time(), bin=bin_n, action="pos", s0=round(float(s0),1),
                       s_achieved=round(float(s1),1) if s1 else None,
                       target=target, lateral_mm=round(float(lat),1) if lat is not None else None,
                       px=[round(float(p2[0])), round(float(p2[1]))] if p2 is not None else None,
                       note=note)
            open(RECORD, "a").write(json.dumps(rec, ensure_ascii=False) + "\n")
            return dict(LAST)
        if cmd == "home":
            p = read_pos()
            if p is None:
                LAST.update(bin=None, msg="带面未见香蕉")
                return dict(LAST)
            s0 = s_of(p)
            step_mm(s0, reverse=True)
            p2 = read_pos()
            s1 = s_of(p2) if p2 is not None else None
            LAST.update(bin=None, msg=f"回黄标: s {s0:.0f} -> {s1:.0f}mm")
            open(RECORD, "a").write(json.dumps(dict(ts=time.time(), bin=0, action="home",
                                                    s0=round(float(s0),1), s_achieved=round(float(s1),1) if s1 else None),
                                              ensure_ascii=False) + "\n")
            return dict(LAST)
        if cmd in ("ok", "bad"):
            p = read_pos()
            s = s_of(p) if p is not None else None
            lat = lat_mm(p) if p is not None else None
            bin_n = LAST.get("bin")
            LAST.update(msg=f"{cmd.upper()} 记录: 仓{bin_n}, s={s:.0f}mm, 横向={lat:+.0f}mm" if s is not None else f"{cmd.upper()} 记录: 未见香蕉")
            open(RECORD, "a").write(json.dumps(dict(ts=time.time(), bin=bin_n, action=cmd,
                                                    s_now=round(float(s),1) if s else None,
                                                    lateral_mm=round(float(lat),1) if lat is not None else None,
                                                    note=note), ensure_ascii=False) + "\n")
            return dict(LAST)
        LAST.update(msg=f"未知指令 {cmd}")
        return dict(LAST)

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>分拣台</title>
<style>
 body{font-family:system-ui,sans-serif;background:var(--card,#111);color:var(--foreground,#eee);margin:0;padding:12px}
 .row{display:flex;gap:12px}
 #wrap{position:relative;width:640px}
 img{width:640px;height:480px;border:1px solid var(--border,#444);border-radius:6px;background:#000}
 .btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
 button{font-size:15px;padding:8px 16px;border-radius:8px;border:1px solid var(--border,#555);
   background:var(--accent,#336);color:inherit;cursor:pointer}
 button:disabled{opacity:.4;cursor:wait}
 #ok{background:#2a6;border-color:#3a7}#bad{background:#a33;border-color:#c44}
 #status{margin-top:8px;font-size:14px;color:var(--muted-foreground,#aaa);min-height:20px}
 input{padding:6px;font-size:13px;width:220px}
</style></head><body>
<div class="row"><div id="wrap"><img src="http://127.0.0.1:8125/stream" alt="live"></div>
<div style="flex:1">
 <div style="font-size:15px;margin-bottom:8px"><b>位置标定</b>：点“测 N 号”→ 步进把香蕉移到 N 号仓接触点（不推杆）→ 你看位置对不对 → OK / BAD</div>
 <div class="btns">
  <button id="p1">测 1 号 (s=120mm)</button>
  <button id="p2">测 2 号 (s=170mm)</button>
  <button id="p3">测 3 号 (s=221mm)</button>
  <button id="home">回黄标 (s=0)</button>
  <button id="ok">OK 位置正确</button>
  <button id="bad">BAD 位置不对</button>
 </div>
 <input id="note" placeholder="备注 (BAD 时说明偏哪/偏多少, 可选)">
 <div id="status">连接中...</div>
</div></div>
<script>
const API = location.origin.startsWith("http") ? location.origin : "http://127.0.0.1:8126";
async function act(cmd){
  const btns=[...document.querySelectorAll("button")];
  btns.forEach(b=>b.disabled=true);
  document.getElementById("status").textContent="执行 "+cmd+" ...";
  try{
    const r = await fetch(API+"/api/action",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({cmd,note:document.getElementById("note").value})});
    const d = await r.json();
    document.getElementById("status").textContent=d.msg||("完成 "+cmd);
  }catch(e){document.getElementById("status").textContent="请求失败: "+e}
  btns.forEach(b=>b.disabled=false);
}
for(const [id,cmd] of [["p1","pos1"],["p2","pos2"],["p3","pos3"],["home","home"],["ok","ok"],["bad","bad"]])
  document.getElementById(id).onclick=()=>act(cmd);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif self.path == "/api/state":
            st = serial_cmd("state", 2.0)
            body = json.dumps(dict(state=st.strip(), last=LAST, record=RECORD),
                              ensure_ascii=False).encode()
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        if self.path != "/api/action":
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get("Content-Length", 0))
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            d = {}
        res = do_action(d.get("cmd", ""), d.get("note", ""))
        body = json.dumps(res, ensure_ascii=False).encode()
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

if __name__ == "__main__":
    B.banana()   # 预热 YOLO + PULLER
    print(f"[bench_web] http://127.0.0.1:{PORT} (记录: {RECORD})", flush=True)
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
