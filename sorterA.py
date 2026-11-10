#!/usr/bin/env python3
"""sorterA: 离散"定位+推"连续模式 (2026-11-04 晚)
用户放香蕉到黄标 -> 场景差分检测放料 -> 步进定位 -> 数据确认 -> 推杆 -> 验证 -> 下一仓
用法: sorterA.py <起始仓 1|2|3> <连续根数 N>
每根 ~10s; 放料检测: 黄标 ROI 相对基线的持续像素差 (手离开后静止才触发)
记录: sort_calibration.jsonl (action="A_run")
"""
import os, sys, time, json, base64, urllib.request
import numpy as np

ARGS = sys.argv[1:]   # 必须在 beltctl import 前抓取 (它要改写 sys.argv)

sys.path.insert(0, "/home/rockets/laya-industrial-bench")
sys.argv = ["beltctl.py", "1"]
import beltctl as B
import paho.mqtt.client as mqtt
import cv2

SERIAL = "/dev/ttyACM0"
RECORD = "/home/rockets/laya-industrial-bench/sort_calibration.jsonl"
REF_EMPTY = "/tmp/sorterA_empty_ref.jpg"   # 空带参考帧 (每轮推完后刷新)
S_TARGET = {1: 120.0, 2: 170.0, 3: 221.0}
STEPS_PER_MM = 69.1
A0 = np.array([598.4, 96.8])
LANE = np.array([-0.707, 0.707])
ANCH = [(0.0, 0.0), (120.0, 139.6), (170.0, 230.1), (220.0, 346.1), (305.0, 545.0)]
ROI = (30, 190, 500, 640)   # y1,y2,x1,x2 黄标区
PLACE_THRESH = 5.0         # 均值像素差
PLACE_SUSTAIN = 5          # 连续 5 次 (~2.5s)
PLACE_TIMEOUT = 300.0      # 每根最长等 5 分钟

def s_of(p):
    d = float((np.array(p) - A0) @ LANE)
    for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
        if d <= d2:
            return s1 + (d - d1) * (s2 - s1) / (d2 - d1)
    return 305.0

FD = os.open(SERIAL, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
time.sleep(3.0)

def cmd(line, wait=3.0):
    os.write(FD, (line + "\n").encode()); t0 = time.time(); out = b""
    while time.time() - t0 < wait:
        try:
            c = os.read(FD, 4096)
            if c: out += c
        except BlockingIOError: time.sleep(0.02)
    return out.decode(errors="replace")

def log(msg):
    print(f"[sorterA {time.strftime('%H:%M:%S')}] {msg}", flush=True)

def fresh_gray_roi():
    for _ in range(6):
        f = B.one_frame()
        if f is not None:
            y1, y2, x1, x2 = ROI
            g = cv2.cvtColor(f[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            return g
        time.sleep(0.4)
    return None

def scene_static():
    """两次新帧 ROI 差异小 -> 手已离开, 画面静止"""
    a = fresh_gray_roi(); time.sleep(0.5); b = fresh_gray_roi()
    if a is None or b is None:
        return False
    return float(np.mean(np.abs(a.astype(int) - b.astype(int)))) < 8.0

def wait_placement(baseline):
    """等用户放香蕉: ROI 相对基线持续变化 + 之后静止"""
    t0 = time.time()
    run = 0
    log("等待放料 ...")
    while time.time() - t0 < PLACE_TIMEOUT:
        g = fresh_gray_roi()
        if g is None:
            time.sleep(0.5); continue
        d = float(np.mean(np.abs(g.astype(int) - baseline.astype(int))))
        if d > PLACE_THRESH:
            run += 1
            if run == PLACE_SUSTAIN and scene_static():
                log(f"检测到放料 (差值 {d:.1f}, 等待 {time.time()-t0:.0f}s)")
                return True
        else:
            run = 0
        time.sleep(0.5)
    log(f"!! 等料超时 {PLACE_TIMEOUT:.0f}s")
    return False

def ref_roi_gray():
    """空带参考帧的 ROI 灰度"""
    ref = cv2.imread(REF_EMPTY)
    if ref is None:
        return None
    y1, y2, x1, x2 = ROI
    return cv2.cvtColor(ref[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)

def update_ref():
    """保存当前 (应为空带) 帧作为参考"""
    for _ in range(4):
        f = B.one_frame()
        if f is not None:
            cv2.imwrite(REF_EMPTY, f)
            log(f"参考帧已刷新 -> {REF_EMPTY}")
            return
        time.sleep(0.4)

def take_baseline():
    """返回当前 ROI 基线 (优先用参考帧, 保证启动时已有香蕉也能检测)"""
    g = ref_roi_gray()
    if g is not None:
        return g
    return take_baseline_live()

def take_baseline_live():
    acc = []
    for _ in range(3):
        g = fresh_gray_roi()
        if g is not None:
            acc.append(g)
        time.sleep(0.4)
    if len(acc) < 2:
        return None
    return np.median(np.stack(acc), axis=0).astype(np.uint8)

def place_and_push(bin_n):
    target = S_TARGET[bin_n]
    p0, _ = B.banana()
    s0 = s_of(p0)
    if s0 > 60:
        log(f"!! 仓{bin_n}: 物体不在起点 (s={s0:.0f}) -> 跳过本轮")
        return False
    ds = target - s0
    steps = int(round(ds * STEPS_PER_MM))
    log(f"仓{bin_n}: 步进 {steps} 步 -> s={target:.0f}")
    cmd(f"stepper {steps}", max(4.0, steps / 3628.0 * 1.5 + 2.0))
    time.sleep(1.5)
    p1, _ = B.banana(); s1 = s_of(p1)
    if abs(s1 - target) > 5:
        st2 = int(round((target - s1) * STEPS_PER_MM))
        log(f"仓{bin_n}: 残差 {s1-target:+.1f}mm -> 自动补 {st2} 步")
        cmd(f"stepper {abs(st2)}{' r' if st2 < 0 else ''}", max(4.0, abs(st2) / 3628.0 * 1.5 + 2.0))
        time.sleep(1.5)
        p1, _ = B.banana(); s1 = s_of(p1)
    if abs(s1 - target) > 8:
        log(f"!! 仓{bin_n}: 定位失败 s={s1:.0f} (目标 {target:.0f}) -> 不推")
        return False
    log(f"仓{bin_n}: 数据确认 到位 s={s1:.1f} (目标 {target:.0f})")
    pre = cmd("state", 2).strip()
    div_pre = int(pre.split("diverted=")[1].split()[0].split("/")[bin_n - 1])
    MQ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sorterA{bin_n}")
    MQ.connect("127.0.0.1", 1883); MQ.loop_start()
    MQ.publish(f"sort/sorter{bin_n}/cmd",
               json.dumps({"job_id": f"A{bin_n}{time.time():.0f}", "action": "divert"}), qos=1).wait_for_publish(2)
    time.sleep(4.0)
    post = cmd("state", 2).strip()
    div_post = int(post.split("diverted=")[1].split()[0].split("/")[bin_n - 1])
    hits = []
    for _ in range(3):
        p, _ = B.banana()
        if p is not None and s_of(p) > 10:
            hits.append(round(s_of(p), 1))
        time.sleep(0.6)
    ok = (div_post > div_pre) and not hits
    log(f"仓{bin_n}: 推杆 {div_pre}->{div_post}, 带面: {hits if hits else '空'} => {'推入成功' if ok else '需目视'}")
    open(RECORD, "a").write(json.dumps(dict(
        ts=round(time.time(), 3), bin=bin_n, action="A_run",
        s0=round(float(s0), 1), s_achieved=round(float(s1), 1), target=target,
        div=f"{div_pre}->{div_post}", result="ok" if ok else "check"), ensure_ascii=False) + "\n")
    return ok

def vision_has_banana():
    """Qwen3.8-27B 宽幅判断带面上有没有香蕉 (首启无参考帧时用)"""
    f = None
    for _ in range(6):
        f = B.one_frame()
        if f is not None:
            break
        time.sleep(0.4)
    if f is None:
        return False
    b64 = base64.b64encode(cv2.imencode(".jpg", cv2.resize(f, (480, 360)))[1]).decode()
    key = [l.split("=", 1)[1].strip() for l in open("/home/rockets/.hermes/.env")
           if l.startswith("HERMES_CUSTOM_DFROBOT_AIGC_API_KEY=")][0]
    payload = {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "传送带分拣台俯视图。带面上（黄色标记附近或带面任何位置）有没有香蕉? 只回答: 有香蕉 / 没有"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
    req = urllib.request.Request("http://192.168.8.56:8080/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=90))
        return "有" in resp["choices"][0]["message"]["content"][:50]
    except Exception as e:
        log(f"视觉检查失败: {repr(e)[:100]}")
        return False

def main():
    if len(ARGS) < 2:
        print("用法: sorterA.py <起始仓 1|2|3> <连续根数 N>")
        return
    start = int(ARGS[0]); n = int(ARGS[1])
    bins = [(start - 1 + i) % 3 + 1 for i in range(n)]
    log(f"=== 连续模式: 仓序 {bins} ===")
    first_placed = False
    if not os.path.exists(REF_EMPTY):
        log("首启无参考帧: 视觉检查带面是否已有香蕉 ...")
        first_placed = vision_has_banana()
        log(f"视觉检查: {'带面有香蕉' if first_placed else '带面无香蕉'}")
    base = None if first_placed else take_baseline()
    for i, bn in enumerate(bins):
        if i == 0 and first_placed:
            log(f"仓{bn}: 已放料, 直接开始")
        else:
            if base is None:
                update_ref(); base = take_baseline()
                if base is None:
                    log("无法取基线 -> 停止"); return
            if not wait_placement(base):
                log(f"仓{bn}: 未等到料 -> 停止")
                break
        time.sleep(0.8)   # 放稳
        ok = place_and_push(bn)
        if not ok:
            log("上一轮需目视确认 -> 暂停")
            break
        update_ref()
        base = take_baseline()
        log(f"第 {i+1}/{n} 根完成, 继续 ...")
    log("=== 结束 ===")

if __name__ == "__main__":
    main()
