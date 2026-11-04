"""3 号仓自动测试循环 v2 (2026-11-04 晚):
时机策略 = 窗口区域判断 + 固定提前量 (用户指定):
  1) 正向 0.0525 m/s; 香蕉进入前窗口 W0 (cx 330-385, cy 270-360) -> 开始轨迹记录
  2) 由轨迹最小二乘拟合 px/s (自标定, 每轮更新)
  3) 目标点 = 上轮触发点 (241.5,400) 沿轨迹上游 15mm (用户反馈"过头 15mm")
  4) 当 香蕉到目标点距离 <= 提前量(0.4s x px/s) 时发 divert -> 扫掠
  5) 验证扫掠发生 (s3 busy); 香蕉出画/失败 -> 反向 stepper 回上游 -> 再来一轮
带面纹理无法光学标定 -> 用香蕉轨迹做参照。
"""
import time, json, os, re
import urllib.request, cv2, numpy as np
from ultralytics import YOLO

SPEED = 0.0525
W0 = (330.0, 385.0, 270.0, 360.0)   # 前窗口: cx_min, cx_max, cy_min, cy_max
LAST_TRIGGER = (241.5, 400.0)       # 扫掠目标点 = 上轮触发点 (用户已确认动作正确)
FEEDBACK_MM = 0.0                   # 15mm 修正已被 LEAD_S 提前触发吸收, 不再叠加
UPSTREAM_VEC = (0.87, -0.50)        # 沿轨迹上游单位向量 (由 (346,340)->(241.5,400) 反推)
LEAD_S = 0.4                        # 提前量: 距目标点还有 0.4s 行程时发 divert
MAX_ATTEMPTS = 6
REV_STEPS = 15000          # ~217mm 反向
REV_WAIT_S = REV_STEPS * 0.4 / 1000.0 + 1.0
SERIAL = "/dev/ttyACM0"
MODEL = '/tmp/banana_train/banana_v1-2/weights/best.pt'
STREAM = 'http://127.0.0.1:8125/stream'
Y = YOLO(MODEL)   # 只加载一次

import paho.mqtt.client as mqtt
MQ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="beltctl2")
MQ.connect("127.0.0.1", 1883)
s3_state = {"v": "ready"}
MQ.on_message = lambda cl, u, m: s3_state.__setitem__(
    "v", json.loads(m.payload.decode()).get("state", "?")) if \
    m.topic == "sort/sorter3/telemetry" else None
MQ.subscribe("sort/sorter3/telemetry", qos=1)
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


def fit_speed(hist):
    """按 首末点直线距离/时间 拟合 px/s; 优先 >=1.0s 长基线."""
    pts = [(t, p[0], p[1]) for t, p in hist if p is not None]
    if len(pts) < 3:
        return None
    for min_dt in (1.0, 0.5):
        seg = [pts[0]] + [q for q in pts[1:] if q[0] - pts[0][0] >= min_dt]
        if len(seg) >= 2:
            a, z = seg[0], seg[-1]
            dt = z[0] - a[0]
            return np.hypot(z[1] - a[1], z[2] - a[2]) / dt
    return None


def main():
    global SERIAL_FD
    SERIAL_FD = os.open(SERIAL, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    time.sleep(3.0)   # DTR 复位: 开串口后等 2.5s+
    serial_cmd("state", 3)
    print(f"[{ts()}] 串口已开, 起步 {SPEED} m/s", flush=True)
    belt("start", SPEED)
    time.sleep(1.0)

    def reverse_home():
        print(f"[{ts()}] 反向回上游 (stepper {REV_STEPS} r)...", flush=True)
        belt("stop")
        serial_cmd(f"stepper {REV_STEPS} r", REV_WAIT_S)
        belt("start", SPEED)

    # ---- 恢复: 等香蕉进前窗口 W0 ----
    pxs = None
    entered = False
    for rec in range(1, 5):
        print(f"[{ts()}] 恢复 {rec}: 等待香蕉进 W0{W0}", flush=True)
        t_wait = time.time()
        last_log = 0.0
        while time.time() - t_wait < 45:
            p, _ = banana()
            if p and W0[0] < p[0] < W0[1] and W0[2] < p[1] < W0[3]:
                print(f"[{ts()}] 恢复 {rec}: 香蕉进 W0 于 {tuple(map(lambda v: round(v), p))}", flush=True)
                entered = True
                break
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
            p, _ = banana()
            if p is None:
                time.sleep(0.3)
                continue
            now = time.time()
            hist.append((now, p))
            if now - last_log > 1.5:
                last_log = now
                pxs_str = f"px/s≈{pxs:.0f}" if pxs else "px/s=?"
                print(f"[{ts()}] {tuple(map(lambda v: round(v), p))} {pxs_str}", flush=True)
            # 接近 W0 后拟合速度并按提前量触发
            near = W0[0] - 60 < p[0] < W0[1] + 80 and W0[2] - 60 < p[1] < W0[3] + 80
            if near:
                pxs = fit_speed(hist) or pxs
                if pxs:
                    px_per_mm = pxs / (SPEED * 1000.0)   # pxs=px/s, SPEED*1000=mm/s
                    target = (LAST_TRIGGER[0] + FEEDBACK_MM * px_per_mm * UPSTREAM_VEC[0],
                              LAST_TRIGGER[1] + FEEDBACK_MM * px_per_mm * UPSTREAM_VEC[1])
                    dist = np.hypot(p[0] - target[0], p[1] - target[1])
                    if dist <= LEAD_S * pxs:
                        print(f"[{ts()}] 到达触发区: 目标 {tuple(map(lambda v: round(v), target))}, "
                              f"距离 {dist:.0f}px <= 提前量 {LEAD_S*pxs:.0f}px -> 发 divert", flush=True)
                        fired = True
                        break
                    if p[0] < 225:
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
        MQ.publish("sort/sorter3/cmd", json.dumps(
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
