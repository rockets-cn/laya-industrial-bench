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
  2 号仓(2026-11-04 相机移动后重新标定): 2 478 295 490 560 150 380
    (推杆接触区在车道 x≈455-480 新坐标, 帧差+实测推动验证; LEAD_S 按仓: 2 号 0.2s)
  取帧: MJPEG 持久连接 StreamPuller (周期 ~0.2s, 2026-11-04 取代每周期新连接 ~1.5s)
"""
import time, json, os, re, sys
import urllib.request, cv2, numpy as np
import torch
torch.set_num_threads(4)   # 限制推理线程: 与 cam_status/mjpeg_server/桌面应用争核时,
                           # 8 线程全开会互相 deschedule 出 1-2s 尖峰 (2026-11-04 实测)
from ultralytics import YOLO

if len(sys.argv) < 2:
    print("用法: beltctl.py <bin 1|2|3>  (时间触发模式, 无默认仓, 防误触)")
    raise SystemExit(2)
BIN = int(sys.argv[1])
assert BIN in (1, 2, 3)
NOMINAL = len(sys.argv) > 2 and sys.argv[2].upper().startswith("N")
SPEED = 0.0525

def _argf(i, default):
    """float argv[i]; NOMINAL 模式 argv[2]='N' 会占位, 解析失败回默认值"""
    if len(sys.argv) > i:
        try:
            return float(sys.argv[i])
        except ValueError:
            return default
    return default

W0 = (_argf(4, 330.0), _argf(5, 385.0), _argf(6, 270.0), _argf(7, 360.0))   # cx_min, cx_max, cy_min, cy_max
LAST_TRIGGER = (_argf(2, 241.5), _argf(3, 400.0))  # 扫掠目标点
FEEDBACK_MM = 0.0                   # 15mm 修正已被 LEAD_S 提前触发吸收, 不再叠加
UPSTREAM_VEC = (0.87, -0.50)        # 沿轨迹上游单位向量 (由 (346,340)->(241.5,400) 反推)
LEAD_S = {1: 0.45, 2: 0.2, 3: 0.35}.get(BIN, 0.4)  # 提前量: 距目标点还有 N s 行程时发 divert
# 2026-11-04 s3a: fit_speed 受 YOLO 抖动影响常给出 50/184 之类的垃圾值;
# 带速是恒速步进驱动的, 实测 89-95px/s -> 拟合不可信时回退默认 95
DEFAULT_PXPS = 95.0
# 2 号接触区窄 (甜区 x≈353,y≈315): 0.2s × 拟合 pxs ≈ 停带滞后, 扫掠时香蕉重心落在甜区 (2026-11-04 实测)
MAX_ATTEMPTS = 6
REV_STEPS = 15000          # ~217mm 反向
REV_WAIT_S = REV_STEPS * 0.4 / 1000.0 + 1.0
SERIAL = "/dev/ttyACM0"
MODEL = '/tmp/banana_train/banana_v1-2/weights/best.pt'
STREAM = 'http://127.0.0.1:8125/stream'
Y = YOLO(MODEL)   # 只加载一次


class StreamPuller:
    """MJPEG 持久连接拉帧器。
    帧格式 (mjpeg_server.py): 每帧一个 part:
        --frame\r\n Content-Type: image/jpeg\r\n X-Timestamp: <ms>\r\n\r\n <JPEG> \r\n
    服务端在线速重发最新帧 -> 客户端按 X-Timestamp 单调递增判新帧;
    连接断开/流重启窗口内自动重连 (带退避), 单帧读取超时 -> 放弃本周期。"""

    def __init__(self, url, max_age=5.0):
        self.url = url
        self.max_age = max_age
        self.resp = None
        self.buf = b""
        self.last_ts = 0

    def _drop(self):
        if self.resp is not None:
            try:
                self.resp.close()
            except Exception:
                pass
            self.resp = None

    def reset(self):
        """流服务重启后调用: 丢弃旧连接, 下次 next_frame 重连新服务。"""
        self._drop()
        self.buf = b""
        self.last_ts = 0

    def _connect(self):
        self._drop()
        self.resp = urllib.request.urlopen(urllib.request.Request(self.url), timeout=20)
        self.buf = b""

    def _parse_one(self):
        """从 buf 解析所有完整 part, 取 ts 最大 (最新) 且 ts > last_ts 的一帧返回。
        其余旧 part 直接丢弃 (s2n 实测: FIFO 语义在 YOLO 节流下积压 ~60 part ≈ 2s 滞后,
        位置读数全是旧的, fit_speed/提前量全部失准)。"""
        best = None  # (ts, f)
        while True:
            hdr = self.buf.find(b"\r\n\r\n")
            if hdr < 0:
                break
            head = self.buf[:hdr]
            body = self.buf[hdr + 4:]
            m = re.search(rb"X-Timestamp: (\d+)", head)
            ts = int(m.group(1)) if m else 0
            e = body.find(b"\r\n--frame")
            if e < 0:
                break
            part = body[:e]
            self.buf = body[e + 9:]
            if ts <= self.last_ts:
                continue
            age = time.time() - ts / 1000.0
            if age > self.max_age:
                continue
            a = part.find(b"\xff\xd8")
            b = part.rfind(b"\xff\xd9")
            if a < 0 or b <= a:
                continue
            f = cv2.imdecode(np.frombuffer(part[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
            if f is None:
                continue
            if best is None or ts > best[0]:
                best = (ts, f)
        if best is not None:
            self.last_ts = best[0]
            return best[1]
        return None

    def next_frame(self, timeout=3.0):
        deadline = time.time() + timeout
        reconnects = 0
        while time.time() < deadline:
            f = self._parse_one()
            if f is not None:
                return f
            if self.resp is None:
                try:
                    self._connect()
                except Exception:
                    reconnects += 1
                    if reconnects >= 5:
                        return None
                    time.sleep(1.0)
                    continue
            try:
                chunk = self.resp.read(32768)
            except Exception:
                self._drop()
                continue
            if not chunk:
                self._drop()
                continue
            self.buf += chunk
            if len(self.buf) > 4 * 1024 * 1024:
                self.buf = self.buf[-1024 * 1024:]   # 防积压
        return None


PULLER = StreamPuller(STREAM)

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
    """取最新帧。MJPEG 持久连接 + X-Timestamp 单调性判新帧 (替代每周期新连接,
    周期从 ~1.5s 降到 ~0.2s, 2026-11-04: W0 通行 0.55s 被旧周期整个跳过是 2 号失准根因)。
    连接失效/流重启窗口内自动重连, 超时返回 None (不炸主循环)。"""
    return PULLER.next_frame(timeout=3.0)


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
    if dt < 0.15:   # 2026-11-04 快速观察下首末点常不足 0.4s; ±20% 速度误差 = 提前量 ±4px, 可接受
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
        PULLER.reset()
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

    def banana_guard():
        """banana() + 旧帧保护 (时间触发版: pHash 循环 + 坐标重现; 正向跳变防护已不需要)"""
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
            matches = 0
            for t_q, h_q, p_q in phash_hist:
                dt = now - t_q
                if not (2.5 <= dt <= 30.0) or hamming(h, h_q) >= 10:
                    continue
                pos_match = (p is None and p_q is None) or (
                    p is not None and p_q is not None and
                    np.hypot(p[0] - p_q[0], p[1] - p_q[1]) < 25)
                if pos_match:
                    matches += 1
            if matches >= 2:
                print(f"[{ts()}] 帧循环检测: 同内容帧 {matches} 次再现 -> 重启流", flush=True)
                stream_restart()
                return None, f
        if p is None:
            stale["n"] = 0
            return None, f
        obs_hist.append((now, p))
        if len(obs_hist) > 60:
            obs_hist.pop(0)
        recent_far = all(np.hypot(p[0]-q[0], p[1]-q[1]) > 40 for _t, q in obs_hist[-2:])
        if recent_far:
            for t_q, q in obs_hist:
                dt = now - t_q
                if 4.0 <= dt <= 45.0 and abs(q[0]-p[0]) < 18 and abs(q[1]-p[1]) < 18:
                    print(f"[{ts()}] 帧循环检测: 坐标 {tuple(map(round, p))} 在 {dt:.0f}s 内重现 -> 重启流", flush=True)
                    stream_restart()
                    return None, f
        return p, f

    # 起步前流健康检查: 帧龄检查 (one_frame 已拒 X-Timestamp >0.6s 的旧帧); 不健康 -> 重启流再查
    # 标称距离模式 (N): 触发全程不依赖相机, 跳过流健康检查
    if not NOMINAL and not stream_healthy():
        print(f"[{ts()}] 起步前流不健康 (疑似 C110 卡帧循环) -> 重启流", flush=True)
        stream_restart()
        if not stream_healthy():
            print(f"[{ts()}] 重启后仍不健康, 停带等待人工 (可重插 USB)", flush=True)
            return

    # ============ 时间触发分拣 (2026-11-04 mm-秒模型, 跑带中不靠 YOLO) ============
    # 真值: v=52.5mm/s (步进计数实测 0.0%偏差), 起步~40ms, L=0.28s (30fps实测)
    # 锚点链 (新机位 2026-11-04, s=0 黄标): P1 s=120 / P2 s=170 / P3 锚 s=220
    #   P3 实际接触 s≈221 (网页定位用户目视 4 轮: 199偏进口10 → 206偏进口5 → 211再来2.5 → 214再来3 → 221 用户"就这个位置"并推入成功; 探针区 202-218 略偏); 远侧推; 掉带点在出口端 (画面外), 严禁正向冲出口
    V_MM_S = 52.5
    L_CONTACT = 0.28
    S_CONTACT = {1: 120.0, 2: 170.0, 3: 221.0}
    S_TRIGGER = S_CONTACT[BIN] - V_MM_S * L_CONTACT   # ≈ -15mm
    STEPS_PER_MM = 69.1
    A0 = np.array([598.4, 96.8])
    LANE_DOWN = np.array([-0.707, 0.707])             # 顺流方向 (单位向量, 新机位 45°)
    ANCH = [(0.0, 0.0), (120.0, 139.6), (170.0, 230.1), (220.0, 346.1), (305.0, 545.0)]  # (s_mm, d_px)

    def d_of(p):
        return float((np.array(p, dtype=float) - A0) @ LANE_DOWN)

    def s_of(p):
        d = d_of(p)
        for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
            if d <= d2:
                return s1 + (d - d1) * (s2 - s1) / (d2 - d1)
        return ANCH[-1][0] + (d - ANCH[-1][1]) * (305.0 - 220.0) / (545.0 - 346.1)

    def local_scale(d):
        for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
            if d <= d2:
                return (d2 - d1) / (s2 - s1)
        return (545.0 - 346.1) / (305.0 - 220.0)

    def on_lane(p):
        """带面判定: 车道投影 s∈[-80,305] 且垂直偏移 <40mm (按局部 px/mm 换算, 透视自适应)"""
        if p is None:
            return False
        d = d_of(p)
        if d < -80 or d > 545:
            return False
        perp_mm = float(np.linalg.norm(np.array(p, dtype=float) - (A0 + LANE_DOWN * d))) / local_scale(d)
        return perp_mm < 40.0

    def stable_pos(max_obs=15):
        """停带, 取 5 次有效观测中位数 (仅接受带面上的香蕉); 不稳定 -> None.
        连续 2 次无新鲜帧 = 流故障 -> 立即重启流 (区别于"带面无香蕉")"""
        ps = []
        no_frame = 0
        seen = 0
        while seen < max_obs and len(ps) < 5:
            seen += 1
            p, f = banana_guard()
            if f is None:
                no_frame += 1
                if no_frame >= 2:
                    print(f"[{ts()}] 连续无新鲜帧 -> 疑似流故障, 重启流", flush=True)
                    stream_restart()
                    no_frame = 0
            else:
                no_frame = 0
                if p is not None and on_lane(p):
                    ps.append(p)
            time.sleep(0.4)
        if len(ps) < 3:
            return None
        c = np.median(ps, axis=0)
        if np.std([d_of(v) for v in ps]) > 20:
            return None
        return (float(c[0]), float(c[1]))

    def step_move(steps, reverse=False):
        """停带时步进移位; >20000 步自动分段 (固件上限)"""
        sign = " r" if reverse else ""
        while steps > 0:
            n = min(steps, 20000)
            print(f"[{ts()}]   步进 {'反' if reverse else '正'}转 {n} 步...", flush=True)
            serial_cmd(f"stepper {n}{sign}", max(10.0, n / 3628.0 * 1.5 + 4.0))
            steps -= n

    def place_s(s_target):
        """把带面上香蕉移到 s_target (±25mm 即止); 无带面香蕉 -> None; 返回 (s, p)"""
        for _ in range(3):
            p = stable_pos()
            if p is None:
                return None
            s0 = s_of(p)
            ds = s_target - s0
            print(f"[{ts()}] 定位: 香蕉 s={s0:.0f}mm -> 目标 {s_target:.0f}mm (Δ={ds:+.0f})", flush=True)
            if abs(ds) < 25:
                return s0, p
            step_move(int(abs(ds) * STEPS_PER_MM), reverse=(ds < 0))
        p = stable_pos()
        return (s_of(p), p) if p is not None else None

    for attempt in range(1, 5):
        s_target = S_TRIGGER - 60.0
        print(f"[{ts()}] === 时间触发轮 {attempt}/4 (仓{BIN}, 触发线 s={S_TRIGGER:.0f}mm{'  [标称距离模式]' if NOMINAL else ''}) ===", flush=True)
        if NOMINAL:
            # 标称距离模式: 香蕉由人工放在黄标 (s=0), 纯步进定位, 全程不碰相机
            print(f"[{ts()}] 标称: 香蕉 s0=0 (黄标), 正转 {s_target:.0f}mm 定位", flush=True)
            step_move(int(round(s_target * STEPS_PER_MM)), reverse=False)
            s0, p_last = 0.0, None
        else:
            placed = place_s(s_target)
            bring = 0
            while placed is None and bring < 8:
                bring += 1
                # 反转运出 (72mm/段): 带面可见区为空, 香蕉必在隐形回路段; 反转让它从出口端背面
                # 浮现到可见带尾, 全程不经过出口端正向转折点 (2026-11-04: 正向 8s 带出多次掉带)
                print(f"[{ts()}] 带面未见香蕉 -> 反转 5000 步带出 (试 {bring}/8)", flush=True)
                serial_cmd("stepper 5000 r", 12.0)
                placed = place_s(s_target)
            if placed is None:
                print(f"[{ts()}] 仍无带面香蕉 -> 停带, 等待人工 (放新香蕉后重跑)", flush=True)
                belt("stop")
                return
            s0, p_last = placed
            # 横向偏移闸 (2026-11-04: 香蕉偏到带面边缘时三仓推杆全部够不到 -> 空推)
            d_chk = d_of(p_last)
            lat_mm = float(np.linalg.norm(np.array(p_last, dtype=float) - (A0 + LANE_DOWN * d_chk))) / local_scale(d_chk)
            print(f"[{ts()}] 横向偏移 {lat_mm:.0f}mm (阈值 15mm)", flush=True)
            if lat_mm > 15:
                print(f"[{ts()}] !! 香蕉偏出推杆可达区 -> 停带, 请重新把香蕉放到带面中间后重跑", flush=True)
                belt("stop")
                return
        if s0 > S_TRIGGER:
            print(f"[{ts()}] 香蕉 s0={s0:.0f}mm 已过触发线, 定位失败 -> 停带等待人工", flush=True)
            belt("stop")
            return

        print(f"[{ts()}] 就位 s0={s0:.0f}mm, 起步", flush=True)
        def div_count(state_str):
            if state_str and "diverted=" in state_str:
                return int(state_str.split("diverted=")[1].split()[0].split("/")[BIN - 1])
            return None

        n_div_pre = div_count(serial_cmd("state", 3))
        t_cmd = time.time()
        MQ.publish("sort/line1/conveyor/cmd",
                   json.dumps({"job_id": f"t{BIN}", "action": "start", "speed_mps": SPEED}), qos=1).wait_for_publish(2)
        t_divert = t_cmd + (S_TRIGGER - s0) / V_MM_S
        print(f"[{ts()}] 预测 divert 于 {t_divert - t_cmd:.3f}s 后", flush=True)
        time.sleep(max(0.0, t_divert - time.time()))
        t_fired = time.time()
        MQ.publish(f"sort/sorter{BIN}/cmd",
                   json.dumps({"job_id": f"td{BIN}", "action": "divert"}), qos=1).wait_for_publish(2)
        print(f"[{ts()}] divert 已发 (计时偏差 {(t_fired - t_divert) * 1000:+.0f}ms)", flush=True)

        t_stop = t_fired + 2.0
        time.sleep(max(0.0, t_stop - time.time()))
        belt("stop")
        time.sleep(1.0)
        s3 = serial_cmd("state", 3)
        n_div_post = div_count(s3)
        if n_div_post is not None and n_div_post == n_div_pre:
            print(f"[{ts()}] !! 推杆未执行 divert (计数 {n_div_pre} 未变; state: {(s3 or '').strip()[-90:]})", flush=True)

        # 验证: 未推中的香蕉应停在 s∈[S_T-25, S_T+170]mm (2s 行程 105mm + 裕量)
        on_belt = None   # None=相机无画面, True=还在带面, False=已离带
        for _ in range(3):
            p, f = banana()
            if f is None:
                time.sleep(0.7)
                continue
            if p is not None and on_lane(p):
                s_obs = s_of(p)
                if S_TRIGGER - 25 <= s_obs <= S_TRIGGER + 170:
                    on_belt = True
                    print(f"[{ts()}] 验证: 香蕉仍在带面 s={s_obs:.0f}mm", flush=True)
            else:
                on_belt = False
            time.sleep(0.7)
        if on_belt is True:
            print(f"[{ts()}] 未推中 -> 下一轮 (定位会自动复位)", flush=True)
            continue
        if on_belt is False:
            print(f"[{ts()}] >>> 仓{BIN} 成功: 香蕉离带面 (已推入仓) <<<", flush=True)
            print(f"[{ts()}] 结果: bin={BIN} attempts={attempt} t_divert_err_ms={(t_fired - t_divert) * 1000:.0f}", flush=True)
            belt("stop")
            return
        print(f"[{ts()}] !! 相机无画面 (C110 卡帧), 无法自动验证 -> 停带, 请目视确认香蕉是否进了仓{BIN} 后告诉我", flush=True)
        belt("stop")
        return
    belt("stop")
    print(f"[{ts()}] 连续失败, 停带等待人工", flush=True)

if __name__ == "__main__":
    main()
