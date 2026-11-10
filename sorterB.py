#!/usr/bin/env python3
"""sorterB: 运动模式连续分拣 (2026-11-04 晚)
带子 0.0525 m/s 连续运行; 用户从进口侧持续放香蕉; YOLO 跟踪每根,
预测到触发线 S_TRIGGER (= 接触点-15mm) 时发 divert 推杆。
轮序 1->2->3->1... 循环, 直到被 kill。
验证: 推杆计数 +1; 未推中(香蕉冲过出口端掉带)记录后同仓再等下一根。
记录: sort_calibration.jsonl (action="B_run")
"""
import os, sys, time, json, subprocess
import numpy as np

ARGS = sys.argv[1:]   # beltctl import 会改写 sys.argv, 提前抓取
sys.path.insert(0, "/home/rockets/laya-industrial-bench")
sys.argv = ["beltctl.py", "1"]
import beltctl as B
import paho.mqtt.client as mqtt
import cv2

V = 52.5
S_CONTACT = {1: 120.0, 2: 170.0, 3: 221.0}
S_TRIGGER = {b: S_CONTACT[b] - 15.0 for b in (1, 2, 3)}
RECORD = "/home/rockets/laya-industrial-bench/sort_calibration.jsonl"
A0 = np.array([598.4, 96.8])
LANE = np.array([-0.707, 0.707])
ANCH = [(0.0, 0.0), (120.0, 139.6), (170.0, 230.1), (220.0, 346.1), (305.0, 545.0)]
MIN_S = 5.0          # 黄标(固定于机架)在 s≈-2.7, 真香蕉 s>5 且 s 递增
CONF = 0.15
LEAD_S = 0.06        # 预测到触发线剩余 <=60ms 时发 divert
WINDOW = 0.5         # s 中值窗口

def s_of(p):
    d = float((np.array(p) - A0) @ LANE)
    for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
        if d <= d2:
            return s1 + (d - d1) * (s2 - s1) / (d2 - d1)
    return 305.0

def log(msg):
    print(f"[sorterB {time.strftime('%H:%M:%S')}] {msg}", flush=True)

FD = os.open("/dev/ttyACM0", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
time.sleep(3.0)

def cmd(line, wait=3.0):
    os.write(FD, (line + "\n").encode()); t0 = time.time(); out = b""
    while time.time() - t0 < wait:
        try:
            c = os.read(FD, 4096)
            if c: out += c
        except BlockingIOError: time.sleep(0.02)
    return out.decode(errors="replace")

def div_count(state_str, b):
    if state_str and "diverted=" in state_str:
        return int(state_str.split("diverted=")[1].split()[0].split("/")[b - 1])
    return None

MQ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sorterB")
MQ.connect("127.0.0.1", 1883)
MQ.loop_start()

def belt_cmd(action, speed=None):
    cmd = {"job_id": f"sorterB-{action}-{time.time():.0f}", "action": action}
    if speed is not None:
        cmd["speed_mps"] = speed
    MQ.publish("sort/line1/conveyor/cmd", json.dumps(cmd), qos=1).wait_for_publish(2)

def divert(b):
    MQ.publish(f"sort/sorter{b}/cmd",
               json.dumps({"job_id": f"B{b}{time.time():.0f}", "action": "divert"}), qos=1).wait_for_publish(2)

def boxes_s(f):
    """YOLO -> 带面上香蕉 s 列表 (排除机架固定黄标 s<5)"""
    if f is None:
        return []
    r = B.Y(f, conf=CONF, iou=0.5, device='cpu', verbose=False)[0]
    if r.boxes is None:
        return []
    out = []
    for xy in r.boxes.xyxy:
        x1, y1, x2, y2 = [int(v) for v in xy]
        s = s_of(((x1 + x2) / 2, (y1 + y2) / 2))
        if s > MIN_S:
            out.append(s)
    return out

def stream_restart():
    log("C110 疑似卡帧 -> 重启流")
    subprocess.run("pkill -f mjpeg_server.py", shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    subprocess.Popen([".venv/bin/python", "/tmp/mjpeg_server.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True,
                     cwd="/home/rockets/laya-industrial-bench")
    time.sleep(5)
    B.PULLER.reset()

def wait_banana():
    """等一根运动中的香蕉出现 (s 递增)"""
    log("等待香蕉上带 ...")
    recent = []
    stall_t0 = None
    while True:
        t = time.time()
        f = B.one_frame()
        if f is None:
            if stall_t0 is None:
                stall_t0 = t
            if t - stall_t0 > 4.0:
                stream_restart(); stall_t0 = None
            continue
        stall_t0 = None
        bs = boxes_s(f)
        if bs:
            s = float(np.median(bs))
            recent.append((t, s))
            recent = [x for x in recent if t - x[0] < 1.5]
            if len(recent) >= 3:
                dt = recent[-1][0] - recent[0][0]
                ds = recent[-1][1] - recent[0][1]
                if dt > 0.4 and ds >= V * dt * 0.5:
                    log(f"检测到香蕉, s≈{s:.0f}mm 且前进中")
                    return recent[-1]
        time.sleep(0.03)

def fire_at_trigger(target, t0, s0):
    """跟踪香蕉, 预测到达触发线时发 divert; 返回 (实际偏差 ms, 最后 s)"""
    hist = [t0]
    s_hist = [s0]
    t = t0
    while True:
        f = B.one_frame()
        bs = boxes_s(f) if f is not None else []
        now = time.time()
        if bs:
            s = float(np.median(bs))
            s_hist.append(s); hist.append(now)
        else:
            # 帧丢失/漏检: 按 52.5mm/s 外推
            last_t, last_s = hist[-1], s_hist[-1]
            s = last_s + V * (now - last_t)
        dt_fire = (target - s) / V
        if dt_fire <= LEAD_S:
            return (dt_fire * 1000.0, s, now)
        if now - t0 > 30:
            return (9999, s, now)
        time.sleep(0.02)

def main():
    state = cmd("state", 2).strip()
    log(f"串口: {state}")
    if "conveyor=stopped" in state:
        belt_cmd("start", 0.0525)
        log("带子启动 0.0525 m/s")
        time.sleep(2.0)
        st2 = cmd("state", 2).strip()
        log(f"确认: {st2}")
    else:
        log("带子已在运行, 直接接管")

    cur = 1
    n_runs = 0
    while True:
        target = S_TRIGGER[cur]
        t0, s0 = wait_banana()
        log(f"仓{cur}: 跟踪中, 触发线 s={target:.0f} (香蕉 s0={s0:.0f})")
        div_pre = div_count(cmd("state", 2), cur)
        err_ms, s_at, t_fire = fire_at_trigger(target, t0, s0)
        divert(cur)
        n_runs += 1
        log(f"仓{cur}: divert 已发 (计时 {err_ms:+.0f}ms, s≈{s_at:.0f})")
        time.sleep(1.2)
        st = cmd("state", 2).strip()
        div_post = div_count(st, cur)
        on_belt = False
        for _ in range(3):
            f = B.one_frame()
            bs = boxes_s(f)
            if bs:
                on_belt = True
            time.sleep(0.4)
        ok = (div_post is not None and div_post > div_pre) and not on_belt
        log(f"仓{cur}: 推杆 {div_pre}->{div_post}, 带面: {'仍有物体' if on_belt else '空'} => "
            f"{'推入成功' if ok else '未推中(香蕉已过, 将掉出口端)'}")
        open(RECORD, "a").write(json.dumps(dict(
            ts=round(time.time(), 3), bin=cur, action="B_run",
            s0=round(float(s0), 1), target=target, t_fire_ms=round(err_ms, 1),
            div=f"{div_pre}->{div_post}",
            result="ok" if ok else "miss"), ensure_ascii=False) + "\n")
        if ok:
            cur = cur % 3 + 1
            log(f"第 {n_runs} 根完成 -> 下一根去 仓{cur}")
        else:
            log(f"下一根仍去 仓{cur} (等你放新香蕉)")

if __name__ == "__main__":
    main()
