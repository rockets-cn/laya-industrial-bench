"""3 号仓自动测试循环 v2 (2026-11-04 晚):
时机策略 = 窗口区域判断 + 固定提前量 (用户指定):
  1) 正向 0.0525 m/s; 香蕉进入前窗口 W0 -> 开始轨迹记录
  2) 由轨迹拟合 px/s (自标定)
  3) 目标点 = 扫掠开始时应到位的点
  4) 当 香蕉到目标点距离 <= 提前量(0.4s x px/s) 时发 divert -> 扫掠
  5) 验证扫掠发生 (sN busy); 香蕉出画/失败 -> 反向 stepper 回上游 -> 再来一轮
用法: beltctl.py [bin] [tx] [ty] [w0minx] [w0maxx] [w0miny] [w0maxy]
  3 号仓默认: 3 241.5 400 330 385 270 360
  1 号仓:     1 390 220 430 510 110 230
  2 号仓:     2 353 315 395 455 215 295   (LEAD_S 按仓: 2 号 0.15s, 其余 0.4s)
"""
import time, json, os, re, sys
import urllib.request, cv2, numpy as np
from ultralytics import YOLO

BIN = int(sys.argv[1]) if len(sys.argv) > 1 else 3
SPEED = 0.0525
W0 = (float(sys.argv[4]) if len(sys.argv) > 4 else 330.0,
      float(sys.argv[5]) if len(sys.argv) > 5 else 385.0,
      float(sys.argv[6]) if len(sys.argv) > 6 else 270.0,
      float(sys.argv[7]) if len(sys.argv) > 7 else 360.0)   # cx_min, cx_max, cy_min, cy_max
LAST_TRIGGER = (float(sys.argv[2]) if len(sys.argv) > 2 else 241.5,
                float(sys.argv[3]) if len(sys.argv) > 3 else 400.0)  # 扫掠目标点
FEEDBACK_MM = 0.0                   # 15mm 修正已被 LEAD_S 提前触发吸收, 不再叠加
UPSTREAM_VEC = (0.87, -0.50)        # 沿轨迹上游单位向量 (由 (346,340)->(241.5,400) 反推)
LEAD_S = {1: 0.4, 2: 0.2, 3: 0.4}.get(BIN, 0.4)  # 提前量: 距目标点还有 N s 行程时发 divert
# 2 号接触区窄 (甜区 x≈353,y≈315): 0.2s × 拟合 pxs ≈ 停带滞后, 扫掠时香蕉重心落在甜区 (2026-11-04 实测)
MAX_ATTEMPTS = 6
REV_STEPS = 15000          # ~217mm 反向
REV_WAIT_S = REV_STEPS * 0.4 / 1000.0 + 1.0
SERIAL = "/dev/ttyACM0"
MODEL = '/tmp/banana_train/banana_v1-2/weights/best.pt'
STREAM = 'http://127.0.0.1:8125/stream'
Y = YOLO(MODEL)   # 只加载一次

import paho.mqtt.client as mqtt
MQ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"beltctl{BIN}")
MQ.connect("127.0.0.1", 1883)
s3_state = {"v": "ready"}
MQ.on_message = lambda cl, u, m: s3_state.__setitem__(
    "v", json.loads(m.payload.decode()).get("state", "?")) if \
    m.topic == f"sort/sorter{BIN}/telemetry" else None
MQ.subscribe(f"sort/sorter{BIN}/telemetry", qos=1)
MQ.loop_start()

T0 = time.time()


def ts():
    return f"{time.time()-T0:7.2f}s"


def belt(action, speed=None):
    cmd = {"job_id": f"beltctl-{action}", "action": action}
    if speed is not None:
        cmd["speed_mps"] = speed
    MQ.publish("sort/line1/conveyor/cmd", json.dumps(cmd), qos=1).wait_for_publish(2)
    time.sleep(1.2)


def serial_cmd(line, wait):
    os.write(SERIAL_FD, (line + "\n").encode())
    t0 = time.time()
    out = b""
    while time.time() - t0 < wait:
        try:
            c = os.read(SERIAL_FD, 4096)
            if c:
                out += c
                if "done" in out.decode(errors="replace"):
                    break
        except BlockingIOError:
            time.sleep(0.05)
    time.sleep(0.5)


def one_frame(max_age=0.6):
    """取最新帧; 若 X-Timestamp 过旧 (C110 冻结) 则稍后重试一次."""
    f = None
    for _ in range(2):
        resp = urllib.request.urlopen(urllib.request.Request(STREAM), timeout=10)
        buf = b""
        end = time.time() + 1.2
        while time.time() < end:
            chunk = resp.read(65536)
            if chunk:
                buf += chunk
            a = buf.find(b"\xff\xd8")
            if a < 0:
                continue
            b = buf.find(b"\xff\xd9", a + 2)
            if b >= 0:
                m = re.findall(rb"X-Timestamp: (\d+)", buf[:a])
                age = (time.time() * 1000 - int(m[-1])) / 1000.0 if m else 0.0
                f = cv2.imdecode(np.frombuffer(buf[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
                buf = buf[b + 2:]
                if f is not None and age < max_age:
                    return f
                if f is not None:
                    break
        time.sleep(0.3)
    return f


def banana():
    f = one_frame()
    if f is None:
        return None, None
    r = Y(f, conf=0.25, iou=0.5, device='cpu', verbose=False)[0]
    if r.boxes is None or len(r.boxes) == 0:
        return None, f
    x1, y1, x2, y2 = [int(v) for v in r.boxes.xyxy[0]]
    return ((x1 + x2) / 2, (y1 + y2) / 2), f


def fit_speed(hist, window=2.5):
    """最近 window 秒内 首末点直线距离/时间 拟合 px/s (滑窗: 抗旧帧垃圾)."""
    pts = [(t, p[0], p[1]) for t, p in hist if p is not None]
    if len(pts) < 3:
        return None
    t_end = pts[-1][0]
    seg = [q for q in pts if q[0] >= t_end - window]
    if len(seg) < 2:
        return None
    a, z = seg[0], seg[-1]
    dt = z[0] - a[0]
    if dt < 0.4:
        return None
    return np.hypot(z[1] - a[1], z[2] - a[2]) / dt


def phash(f):
    """感知哈希 (32x32 DCT 低频 -> 255 bit): 同内容不同编码也一致, 抗 JPEG 重编码."""
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (32, 32), interpolation=cv2.INTER_AREA)
    d = cv2.dct(np.float32(g))
    low = d[:16, :16]
    med = np.median(low[1:, 1:])
    return (low[1:, 1:] > med).flatten().astype(np.uint8)


def hamming(a, b):
    return int((a ^ b).sum())


def main():
    global SERIAL_FD
    SERIAL_FD = os.open(SERIAL, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    time.sleep(3.0)   # DTR 复位: 开串口后等 2.5s+
    serial_cmd("state", 3)

    import subprocess

    stale = {"n": 0, "last_pc": None}
    obs_hist = []   # (t, p) 近期观测, 用于重复检测
    phash_hist = []   # (t, phash) 内容指纹: 同内容帧 3-30s 内再现 = 循环旧帧 (铁证)

    def stream_restart():
        print(f"[{ts()}] 重启流服务 (C110 卡帧)...", flush=True)
        subprocess.run("pkill -f mjpeg_server.py", shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        subprocess.Popen([".venv/bin/python", "/tmp/mjpeg_server.py"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True,
                         cwd="/home/rockets/laya-industrial-bench")
        time.sleep(5)
        stale["n"] = 0
        stale["last_pc"] = None
        obs_hist.clear()
        phash_hist.clear()

    def stream_healthy():
        """起步前: 3 次取帧 (one_frame 已拒 X-Timestamp >0.6s 的旧帧) 至少 2 次成功 = 相机活着.
        卡帧循环态 (重放旧帧) 时间戳照样新鲜, 此处不拦; 由跑带中的 pHash 循环检测兜底."""
        ok = 0
        for _ in range(3):
            if one_frame() is not None:
                ok += 1
            time.sleep(1.5)
        return ok >= 2

    # 起步前流健康检查: 帧龄检查 (one_frame 拒 >0.6s 旧帧); 不健康 -> 重启流再查
    if not stream_healthy():
        print(f"[{ts()}] 起步前流不健康 (疑似 C110 卡帧循环) -> 重启流", flush=True)
        stream_restart()
        if not stream_healthy():
            print(f"[{ts()}] 重启后仍不健康, 停带等待人工 (可重插 USB)", flush=True)
            return

    print(f"[{ts()}] 串口已开, 起步 {SPEED} m/s", flush=True)
    belt("start", SPEED)
    time.sleep(1.0)

    def reverse_home():
        print(f"[{ts()}] 反向回上游 (stepper {REV_STEPS} r)...", flush=True)
        fwd["v"] = False
        belt("stop")
        serial_cmd(f"stepper {REV_STEPS} r", REV_WAIT_S)
        belt("start", SPEED)
        fwd["v"] = True

    # ---- 旧帧循环保护 (C110 卡帧时 USB 会循环发旧帧, 坐标乱跳) ----

    def banana_guard():
        """banana() + 旧帧保护:
        1) 同一 JPEG 内容 2-30s 内再现 -> 帧循环 -> 立即重启流
        2) 同一坐标在 4-45s 内重现且远离最近观测 -> 帧循环 -> 重启流
        3) 正向跑带时坐标上游跳变 -> 丢弃该帧; 连跳 3 次重启流
        """
        p, f = banana()
        if p is None and f is None:
            stale["n"] = 0
            return p, f
        now = time.time()
        if f is not None:
            h = phash(f)
            phash_hist.append((now, h, p))
            if len(phash_hist) > 120:
                phash_hist.pop(0)
            for t_q, h_q, p_q in phash_hist:
                dt = now - t_q
                if not (2.5 <= dt <= 30.0) or hamming(h, h_q) >= 30:
                    continue
                # 位置也要一致 (或都无香蕉): 排除静止带面纹理的 pHash 相似
                pos_match = (p is None and p_q is None) or (
                    p is not None and p_q is not None and
                    np.hypot(p[0] - p_q[0], p[1] - p_q[1]) < 25)
                if pos_match:
                    print(f"[{ts()}] 帧循环检测: 同内容帧在 {dt:.0f}s 内再现 -> 重启流",
                          flush=True)
                    stream_restart()
                    return None, f
        if p is None:   # 有帧但没香蕉: 位置检查跳过, 内容检查已完成
            stale["n"] = 0
            return None, f
        obs_hist.append((now, p))
        if len(obs_hist) > 60:
            obs_hist.pop(0)
        # 重复检测: 4-45s 前有过几乎相同位置且已远离最近观测 = 循环旧帧
        if fwd["v"]:
            recent_far = all(
                np.hypot(p[0] - q[0], p[1] - q[1]) > 40 for _t, q in obs_hist[-2:])
            if recent_far:
                for t_q, q in obs_hist:
                    dt = now - t_q
                    if 4.0 <= dt <= 45.0 and abs(q[0] - p[0]) < 18 and abs(q[1] - p[1]) < 18:
                        print(f"[{ts()}] 帧循环检测: 坐标 {tuple(map(round, p))} 在 {dt:.0f}s 内重现 -> 重启流",
                              flush=True)
                        stream_restart()
                        return None, f
        pc = p[0] * UPSTREAM_VEC[0] + p[1] * UPSTREAM_VEC[1]   # 越大越上游
        if fwd["v"] and stale["last_pc"] is not None and pc > stale["last_pc"] + 55:
            stale["n"] += 1
            print(f"[{ts()}] 疑似旧帧 (坐标上游跳变 {stale['n']}/3)", flush=True)
            if stale["n"] >= 3:
                stream_restart()
            stale["last_pc"] = pc
            return None, f
        stale["n"] = 0
        stale["last_pc"] = pc
        return p, f

    fwd = {"v": True}

    # ---- 恢复: 等香蕉进前窗口 W0 ----
    pxs = None
    entered = False
    for rec in range(1, 5):
        print(f"[{ts()}] 恢复 {rec}: 等待香蕉进 W0{W0}", flush=True)
        t_wait = time.time()
        last_log = 0.0
        last_in_w0 = None
        while time.time() - t_wait < 45:
            p, _ = banana_guard()
            if p and W0[0] < p[0] < W0[1] and W0[2] < p[1] < W0[3]:
                if last_in_w0 is not None and time.time() - last_in_w0 < 2.5:
                    print(f"[{ts()}] 恢复 {rec}: 香蕉进 W0 于 {tuple(map(lambda v: round(v), p))}", flush=True)
                    entered = True
                    break
                last_in_w0 = time.time()
            else:
                last_in_w0 = None
            if time.time() - last_log > 3:
                last_log = time.time()
                print(f"[{ts()}] " + (f"香蕉 {tuple(map(lambda v: round(v), p))} (等进 W0...)" if p else "画面无香蕉"),
                      flush=True)
            time.sleep(0.2)
        if entered:
            break
        print(f"[{ts()}] 恢复 {rec}: 超时 -> 反向试探", flush=True)
        reverse_home()
    else:
        print(f"[{ts()}] 4 轮恢复失败 (香蕉可能掉带外), 停带等待人工", flush=True)
        belt("stop")
        return

    # ---- 测试轮 ----
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"[{ts()}] === 第 {attempt}/{MAX_ATTEMPTS} 轮: 轨迹记录 + 提前量触发 ===", flush=True)
        hist = []
        fired = False
        t_wait = time.time()
        last_log = 0.0
        while time.time() - t_wait < 60:
            p, _ = banana_guard()
            if p is None:
                time.sleep(0.3)
                continue
            now = time.time()
            hist.append((now, p))
            if now - last_log > 1.5:
                last_log = now
                pxs_str = f"px/s≈{pxs:.0f}" if pxs else "px/s=?"
                print(f"[{ts()}] {tuple(map(lambda v: round(v), p))} {pxs_str}", flush=True)
            # 全程拟合速度并按提前量触发 (滑窗拟合; 距离 <= 提前量即发 divert)
            pxs = fit_speed(hist) or pxs
            if pxs and pxs > 20:
                px_per_mm = pxs / (SPEED * 1000.0)   # pxs=px/s, SPEED*1000=mm/s
                target = (LAST_TRIGGER[0] + FEEDBACK_MM * px_per_mm * UPSTREAM_VEC[0],
                          LAST_TRIGGER[1] + FEEDBACK_MM * px_per_mm * UPSTREAM_VEC[1])
                dist = np.hypot(p[0] - target[0], p[1] - target[1])
                if dist <= LEAD_S * pxs:
                    print(f"[{ts()}] 到达触发区: 目标 {tuple(map(lambda v: round(v), target))}, "
                          f"距离 {dist:.0f}px <= 提前量 {LEAD_S*pxs:.0f}px -> 发 divert", flush=True)
                    fired = True
                    break
                if p[0] < LAST_TRIGGER[0] - 40:
                    print(f"[{ts()}] 香蕉已过目标 -> 兜底触发", flush=True)
                    fired = True
                    break
        if not fired:
            print(f"[{ts()}] 本轮未触发 (香蕉丢失/出画) -> 反向重试", flush=True)
            reverse_home()
            continue
        for _ in range(20):
            if s3_state["v"] in ("ready", "busy"):
                break
            time.sleep(0.5)
        MQ.publish(f"sort/sorter{BIN}/cmd", json.dumps(
            {"job_id": f"beltctl-a{attempt}", "action": "divert",
             "material": "banana", "qty": 1, "trigger": "immediate"}), qos=1).wait_for_publish(2)
        print(f"[{ts()}] divert 已发, 等扫掠...", flush=True)
        saw_busy = False
        for _ in range(30):
            if s3_state["v"] == "busy":
                saw_busy = True
            time.sleep(0.3)
        time.sleep(2.0)
        p2, _ = banana()
        pos2 = tuple(map(lambda v: round(v), p2)) if p2 else "出画"
        print(f"[{ts()}] 扫掠 busy={saw_busy}, 扫掠后香蕉: {pos2}", flush=True)
        if saw_busy:
            if pxs:
                print(f"[{ts()}] >>> 第 {attempt} 轮完成: 窗口+提前量触发, 扫掠发生; "
                      f"标定 {pxs:.1f} px/s = {pxs/SPEED/1000:.2f} px/mm <<<", flush=True)
            else:
                print(f"[{ts()}] >>> 第 {attempt} 轮完成: 窗口+提前量触发, 扫掠发生 <<<", flush=True)
            belt("stop")
            print(f"[{ts()}] 停带, 结束", flush=True)
            return
        print(f"[{ts()}] 扫掠未发生 -> 反向重试", flush=True)
        reverse_home()
    print(f"[{ts()}] 达到最大轮数, 结束", flush=True)
    belt("stop")


if __name__ == "__main__":
    main()
