#!/usr/bin/env python3
"""speed_probe: 纯测速 — 跟踪带面上每根香蕉, 算 s-t 斜率 (mm/s)
不动带子, 不推杆, 只观测. 用于验证速度一致性 (用户 3 次放料取平均).
"""
import os, sys, time
import numpy as np
import torch

sys.path.insert(0, "/home/rockets/laya-industrial-bench")
sys.argv = ["beltctl.py", "1"]
import beltctl as B

torch.set_num_threads(2)
A0 = np.array([598.4, 96.8]); LANE = np.array([-0.707, 0.707])
ANCH = [(0.0,0.0),(120.0,139.6),(170.0,230.1),(220.0,346.1),(305.0,545.0)]
def s_of(p):
    d = float((np.array(p)-A0)@LANE)
    for (s1,d1),(s2,d2) in zip(ANCH,ANCH[1:]):
        if d<=d2: return s1+(d-d1)*(s2-s1)/(d2-d1)
    return 305.0

def log(m): print(f"[probe {time.strftime('%H:%M:%S')}] {m}", flush=True)

def scan():
    f = B.one_frame()
    if f is None: return []
    r = B.Y(f, conf=0.10, iou=0.5, device='cpu', verbose=False, imgsz=320)[0]
    if r.boxes is None: return []
    out = []
    for i, xy in enumerate(r.boxes.xyxy):
        x1,y1,x2,y2 = [int(v) for v in xy]
        p = ((x1+x2)/2.0, (y1+y2)/2.0)
        out.append((s_of(p), p, float(r.boxes.conf[i])))
    return out

log("测速程序启动 (只观测, 不动带子/推杆)")
V_REF = 50.0/7.82
run = 0
results = []
while True:
    dets = scan()
    moving = [(s,p,c) for s,p,c in dets if -5 <= s <= 285 and c >= 0.20]
    if not moving:
        time.sleep(0.3)
        continue
    # 跟踪一根: 取置信度最高的
    s, p, c = max(moving, key=lambda x: x[2])
    run += 1
    log(f"--- 第{run}根: s0={s:.0f}mm conf={c:.2f}, 开始跟踪 ---")
    track = [(time.time(), s, p)]
    last_match = time.time()
    deadline = time.time() + 60.0
    while time.time() < deadline:
        time.sleep(0.25)
        dets2 = scan()
        match = None
        for s2, p2, c2 in dets2:
            if c2 >= 0.15 and abs(p2[0]-p[0]) < 35 and abs(p2[1]-p[1]) < 35:
                match = (s2, p2, c2); break
        now = time.time()
        if match:
            s, p, c = match
            track.append((now, s, p))
            last_match = now
        elif now - last_match > 2.0:
            break
        if s > 290:
            log(f"香蕉出画面 (s={s:.0f})")
            break
    # 计算速度: 取首尾 80% 段做线性回归
    if len(track) >= 6:
        t = np.array([x[0] for x in track]); sarr = np.array([x[1] for x in track])
        k = max(2, len(track)//5)
        sel = slice(k, len(track)-k)
        coef = np.polyfit(t[sel], sarr[sel], 1)
        v = float(coef[0])
        results.append(v)
        log(f"第{run}根: {len(track)}个样本, 速度 = {v:.2f} mm/s (参考 {V_REF:.3f}, 偏差 {(v-V_REF)/V_REF*100:+.1f}%)")
        if len(results) >= 3:
            avg = sum(results)/len(results)
            log(f"=== 3根平均: {avg:.2f} mm/s, 逐根: {['%.2f'%r for r in results]} ===")
            break
    else:
        log(f"第{run}根: 跟踪样本不足({len(track)}), 跳过")
    time.sleep(2.0)
