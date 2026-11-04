#!/usr/bin/env python3
"""sorterD: 运动进带 + 带停直推 (用户逻辑, v3)
逻辑:
  1. 带子连续跑, YOLO 盯进口区找香蕉
  2. 锁定香蕉, 用香蕉自己的实测速度 V_est (不信标称值) 预测位置
  3. 选目标仓 (循环 1->2->3, 取下一个够得着的接触点)
  4. 预测到达 (S_target - D_OVER) 时停带 (固件瞬停; 运行中改速不被接受, 无梯形减速)
  5. 停稳实测落点, 偏差>10mm 正步微调
  6. 带停直推 -> 双验证 (计数器+推前后帧差: 香蕉确实挪了) -> 带子继续 -> 下一仓
"""
import os, sys, time, json
import numpy as np
import torch

ARGS = sys.argv[1:]
sys.path.insert(0, "/home/rockets/laya-industrial-bench")
sys.argv = ["beltctl.py", "1"]
import beltctl as B
import paho.mqtt.client as mqtt

torch.set_num_threads(2)
IMG_SZ = 320

V_NOM = 52.5   # 20:17 实测 3 根平均 52.67mm/s, 与设计值/计数口径 (10923 ticks/s ÷ 207.3 ticks/mm) 双重吻合
S_TARGET = {1: 120.0, 2: 170.0, 3: 221.0}
STEPS_PER_MM = 69.1
RECORD = "/home/rockets/laya-industrial-bench/sort_calibration.jsonl"
A0 = np.array([598.4, 96.8])
LANE = np.array([-0.707, 0.707])
ANCH = [(0.0, 0.0), (120.0, 139.6), (170.0, 230.1), (220.0, 346.1), (305.0, 545.0)]

D_OVER = {1: 2.0, 2: 2.0, 3: 2.0}   # 每仓独立停带提前量, 各自学该仓的停带后滚动
MIN_APPROACH = 15.0
TOL_PUSH = 10.0

def s_of(p):
    d = float((np.array(p) - A0) @ LANE)
    for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
        if d <= d2:
            return s1 + (d - d1) * (s2 - s1) / (d2 - d1)
    return 305.0

def px_of(s):
    """s -> 像素坐标 (s_of 的逆)"""
    s = max(0.0, min(305.0, s))
    d = None
    for i in range(len(ANCH) - 1):
        (sa, da), (sb, db) = ANCH[i], ANCH[i + 1]
        if sa <= s <= sb:
            d = da + (s - sa) * (db - da) / (sb - sa)
            break
    if d is None:
        d = ANCH[-1][1]
    return (float(A0[0] + d * LANE[0]), float(A0[1] + d * LANE[1]))

def log(msg):
    print(f"[sorterD {time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------------- 串口 ----------------
FD = os.open("/dev/ttyACM0", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
time.sleep(3.0)

def cmd(line, wait=3.0):
    """发串口命令; 收到数据后 0.15s 无新数据即返回 (不再死等满 wait)"""
    os.write(FD, (line + "\n").encode())
    t0 = time.time(); out = b""; last_rx = None
    while time.time() - t0 < wait:
        try:
            c = os.read(FD, 4096)
            if c:
                out += c; last_rx = time.time()
        except BlockingIOError:
            pass
        if last_rx is not None and time.time() - last_rx > 0.15:
            break
        time.sleep(0.01)
    return out.decode(errors="replace")

def state_line():
    st = cmd("state", 2.0).strip().split("\n")
    for line in reversed(st):
        if "conveyor=" in line:
            return line
    return st[-1] if st else ""

def div_count(line, b):
    if "diverted=" in line:
        return int(line.split("diverted=")[1].split()[0].split("/")[b - 1])
    return None

def step_mm(mm):
    n = max(1, int(mm * STEPS_PER_MM))
    cmd(f"stepper {n}", wait=max(3.0, mm * 0.15))
    log(f"正步 {n} 步 (~{mm:.1f}mm)")

# ---------------- MQTT ----------------
MQ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sorterD")
MQ.connect("127.0.0.1", 1883)
MQ.loop_start()

def mqtt_pub(topic, payload):
    for _ in range(3):
        try:
            MQ.publish(topic, json.dumps(payload), qos=1).wait_for_publish(2)
            return
        except Exception:
            try: MQ.reconnect()
            except Exception: time.sleep(0.5)

def belt(action, speed=None):
    c = {"job_id": f"sorterD-{action}-{time.time():.0f}", "action": action}
    if speed is not None:
        c["speed_mps"] = speed
    mqtt_pub("sort/line1/conveyor/cmd", c)

def divert(b):
    mqtt_pub(f"sort/sorter{b}/cmd", {"job_id": f"D{b}{time.time():.0f}", "action": "divert"})

# ---------------- 视觉 ----------------
PERP = np.array([0.707, 0.707])   # 垂直于 LANE
LANE_HALF_W = 60.0                # 带面半宽(px): 超出即带外物体 (滑槽旧香蕉/背景), 一律忽略

def detect(f):
    if f is None:
        return []
    r = B.Y(f, conf=0.10, iou=0.5, device='cpu', verbose=False, imgsz=IMG_SZ)[0]
    if r.boxes is None:
        return []
    out = []
    for i, xy in enumerate(r.boxes.xyxy):
        x1, y1, x2, y2 = [int(v) for v in xy]
        p = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        if abs((np.array(p) - A0) @ PERP) > LANE_HALF_W:
            continue   # 落点污染过滤: 只认带面上的
        out.append((s_of(p), p, float(r.boxes.conf[i])))
    return out

blacklist = []
static_cand = {}

def is_blacklisted(p, tol=10):
    return any(abs(p[0] - x) < tol and abs(p[1] - y) < tol for x, y, _, _ in blacklist)

def static_update(dets, now):
    seen = set()
    for s, p, c in dets:
        if c < 0.30:
            continue
        k = (int(p[0]) // 8, int(p[1]) // 8)
        seen.add(k)
        e = static_cand.get(k)
        if e is None:
            static_cand[k] = (now, 1, s, p)
        else:
            ft, cnt, s0, p0 = e
            if now - ft > 1.0 and cnt >= 3:
                blacklist.append((*p0, s0, now))
                static_cand.pop(k, None)
                log(f"拉黑静止物体: s={s0:.0f}mm ({int(p0[0])},{int(p0[1])})")
            else:
                static_cand[k] = (ft, cnt + 1, s, p)
    for k in list(static_cand):
        if k not in seen and now - static_cand[k][0] > 5.0:
            static_cand.pop(k)
    if len(blacklist) > 40:
        del blacklist[:20]

def measure_rest(sp_pred, max_wait=1.6):
    """停带后等香蕉滚稳 (连续两次读数差 <6mm), 返回静止 s; 取离 sp_pred 最近的检测"""
    t_end = time.time() + max_wait
    prev = None
    while time.time() < t_end:
        f = B.one_frame()
        cands_r = [s for s, p, c in detect(f)
                   if c >= 0.15 and not is_blacklisted(p) and abs(s - sp_pred) < 70]
        sf = min(cands_r, key=lambda s: abs(s - sp_pred)) if cands_r else None
        if sf is not None and prev is not None and abs(sf - prev) < 6:
            return sf
        if sf is not None:
            prev = sf
        time.sleep(0.35)
    return prev

def follow_px(dets, ref_px, max_jump=35):
    """在 ref_px 附近(欧氏距离<=max_jump)找同一根香蕉 -> (s, px) 或 None (跳变限幅, 防误抓其它香蕉)"""
    if ref_px is None:
        return None
    best = None; bestd = 1e9
    for s, p, c in dets:
        if c < 0.15 or is_blacklisted(p):
            continue
        dx = p[0] - ref_px[0]; dy = p[1] - ref_px[1]
        d = (dx * dx + dy * dy) ** 0.5
        if d < bestd:
            bestd = d; best = (s, p)
    return best if (best is not None and bestd <= max_jump) else None

def measure_rest_px(ref_px, max_wait=1.8, max_jump=50):
    """从 ref_px 身份跟随同一根香蕉, 等它滚稳 (相邻两次 s 差<6mm), 返回静止 s 或 None"""
    t_end = time.time() + max_wait
    ref = ref_px; prev_s = None; first = True
    while time.time() < t_end:
        f = B.one_frame()
        if f is not None:
            mj = 90 if first else max_jump     # 停带瞬间香蕉可能已冲出原位置, 首次放宽重捕
            m = follow_px(detect(f), ref, max_jump=mj)
            if m is not None:
                s, px = m
                if prev_s is not None and abs(s - prev_s) < 6:
                    return s
                prev_s = s; ref = px
            first = False
        time.sleep(0.35)
    return prev_s

def measure_s(n=5, s_range=None, px_ref=None):
    """带停状态下实测香蕉位置 (中值)
    px_ref: 参考像素点 — 只取距离它 40px 内的框 (区分带面新香蕉和滑槽里的旧香蕉)"""
    ss = []
    for _ in range(n):
        f = B.one_frame()
        for s, p, c in detect(f):
            if c >= 0.15 and not is_blacklisted(p):
                if s_range is not None and not (s_range[0] <= s <= s_range[1]):
                    continue
                if px_ref is not None:
                    if abs(p[0] - px_ref[0]) > 40 or abs(p[1] - px_ref[1]) > 40:
                        continue
                ss.append(s)
        time.sleep(0.15)
    return float(np.median(ss)) if ss else None

def roi_moved(px_center, pre, post, half=70, thr=30):
    """推前后帧差: 香蕉是否物理挪动了 (从带面进滑槽)"""
    x, y = int(px_center[0]), int(px_center[1])
    x0, x1 = max(0, x - half), min(pre.shape[1], x + half)
    y0, y1 = max(0, y - half), min(pre.shape[0], y + half)
    a = np.abs(cv2_gray(pre[y0:y1, x0:x1]) - cv2_gray(post[y0:y1, x0:x1]))
    return int((a > thr).sum())

import cv2
def cv2_gray(img):
    return cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(float)

# ---------------- 主循环 ----------------
def main():
    global D_OVER
    st = state_line()
    log(f"串口: {st}")
    if "conveyor=stopped" in st:
        belt("start", 0.0525)
        time.sleep(2.0)
        log("带子已启动: " + state_line())
    log(f"目标仓: 1={S_TARGET[1]:.0f} 2={S_TARGET[2]:.0f} 3={S_TARGET[3]:.0f}mm, 提前量 D_OVER={D_OVER[1]:.1f}/{D_OVER[2]:.1f}/{D_OVER[3]:.1f}mm")

    cur = 1
    cands = []
    while True:
        try:
            now = time.time()
            f = B.one_frame()
            if f is None:
                time.sleep(0.1)
                continue
            dets = detect(f)
            static_update(dets, now)

            # ---- 找料 (IDLE): 进口区运动香蕉; 锁最靠前一根 (前者优先处理) ----
            for s, p, c in dets:
                if -8 <= s <= 150 and c >= 0.15 and not is_blacklisted(p):
                    cands.append((now, s, p))
            cands = [x for x in cands if now - x[0] < 1.5]
            if len(cands) >= 4:
                dt = cands[-1][0] - cands[0][0]
                if dt > 0.6 and cands[-1][1] - cands[0][1] > 8:   # 窗口内有前向运动
                    front = max(cands, key=lambda x: x[1])        # 最靠前一根
                    s0 = float(front[1]); lock_px = front[2]; t0 = now
                    v_est = (cands[-1][1] - cands[0][1]) / dt
                    cands = []
                    log(f"香蕉锁定 s0={s0:.0f}mm (身份跟踪启动, px=({int(lock_px[0])},{int(lock_px[1])}))")
                    sp = s0
                    # 选目标: 循环序里第一个够得着的接触点
                    tbin = None
                    for k in range(3):
                        b = ((cur - 1 + k) % 3) + 1
                        if S_TARGET[b] - D_OVER[b] > s0 + MIN_APPROACH:
                            tbin = b
                            break
                    if tbin is None:
                        log(f"!! 香蕉太靠后 (s0={s0:.0f}), 追不上任何仓 -> 放行")
                        continue
                    target = S_TARGET[tbin]
                    log(f"=== 目标 仓{tbin} (s{target:.0f}), 停带点 s{target - D_OVER[tbin]:.0f} ===")
                    # ---- 接近: 身份跟随锁定香蕉, 到停带点 -> 停 ----
                    trk_px = lock_px
                    while True:
                        n2 = time.time()
                        sp = s0 + V_NOM * (n2 - t0)
                        f2 = B.one_frame()
                        if f2 is not None:
                            m = follow_px(detect(f2), trk_px, max_jump=35)
                            if m is not None:
                                trk_px = m[1]
                        if sp >= target - D_OVER[tbin]:
                            belt("stop")
                            log(f"停带指令已发 (s_pred={sp:.0f}mm)")
                            break
                        time.sleep(0.02)
                    # ---- 等锁定香蕉滚稳 -> 测真实落点 -> 够不着就补步 -> 推 ----
                    sf = measure_rest_px(trk_px)
                    if sf is None:
                        log("!! 停带后没找到香蕉 -> 恢复带子")
                        belt("start", 0.0525); time.sleep(0.5); continue
                    err = sf - target
                    D_OVER[tbin] = min(15.0, max(0.0, D_OVER[tbin] + 0.3 * err))   # 每仓独立, 封顶15防停太早
                    log(f"落点 {sf:.1f} (偏差 {err:+.1f}) -> 仓{tbin}提前量 D_OVER={D_OVER[tbin]:.1f}")
                    if err < -22:        # 短得推杆够不着 -> 正步补 (实测 ~57 步/mm)
                        need = min(target - sf, 50.0)
                        n_steps = int(need * 57)
                        cmd(f"stepper {n_steps}", wait=max(2.0, need * 0.1))
                        log(f"正步补 {n_steps} 步 (~{need:.0f}mm)")
                        time.sleep(0.3)
                    elif err > 25:       # 滚过推杆触及范围 -> 放行, 让它去下一仓
                        log("!! 滚过触及范围 -> 放行"); belt("start", 0.0525); time.sleep(0.5); continue
                    pre_f = B.one_frame()
                    pre = div_count(state_line(), tbin)
                    divert(tbin)
                    post = pre
                    t_end = time.time() + 4.0
                    while time.time() < t_end:      # 计数器一变即推完, 不死等
                        time.sleep(0.25)
                        post = div_count(state_line(), tbin)
                        if pre is not None and post is not None and post > pre:
                            break
                    belt("start", 0.0525)           # 推完立即恢复带子 (用户定稿)
                    time.sleep(0.5)                 # 香蕉扫进滑槽 (验证用, 不挡带子)
                    moved = (pre is not None and post is not None and post > pre)
                    post_f = B.one_frame()
                    moved_px = roi_moved(px_of(target), pre_f, post_f) if (pre_f is not None and post_f is not None) else -1
                    ok = moved and moved_px > 50
                    log(f"仓{tbin}: 推杆 {pre}->{post}{'(执行)' if moved else '(未执行!)'}, "
                        f"帧差 {moved_px}px => {'推入成功' if ok else '未推中'}")
                    open(RECORD, "a").write(json.dumps(dict(
                        ts=round(time.time(), 3), bin=tbin, action="D_run",
                        s0=round(s0, 1), v_est=round(v_est, 1), target=round(target, 1),
                        div=f"{pre}->{post}", D_over=round(D_OVER[tbin], 1),
                        result="ok" if ok else "miss"), ensure_ascii=False) + "\n")
                    # ---- 目标轮换 (带子已在推完时恢复) ----
                    if ok:
                        cur = (cur % 3) + 1
                        log(f"带子已恢复, 下一根去 仓{cur}")
                    else:
                        log(f"带子已恢复, 下一根仍去 仓{tbin}")
            time.sleep(0.02)
        except Exception as e:
            log(f"异常(继续): {repr(e)[:150]}")
            time.sleep(0.5)

if __name__ == "__main__":
    main()
