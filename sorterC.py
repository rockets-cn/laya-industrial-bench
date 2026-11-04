#!/usr/bin/env python3
"""sorterC: 运动模式连续分拣 v2 (2026-11-04 晚, 重写)
设计铁律:
  1. 带速 52.5mm/s 是计时真值 (步进驱动, 实测误差<1%);
     YOLO 只负责"找到香蕉 + 确认是它", 不做计时源
  2. 单香蕉轨迹门控: 只接受与预测位置吻合(±15mm)的框;
     静止物体 (仓里香蕉/机架黄标) 自动进黑名单, 永不参与判断
  3. 状态机: IDLE(等料) -> TRACKING(锁定 t0,s0) -> FIRED -> VERIFY -> 换仓
     发射时刻纯定时: t_fire = t0 + (S_TRIGGER - s0)/V
  4. 任何异常只记日志, 不杀进程
用法: sorterC.py            (1->2->3 循环, 直到 kill)
"""
import os, sys, time, json
import numpy as np
import torch

ARGS = sys.argv[1:]
sys.path.insert(0, "/home/rockets/laya-industrial-bench")
sys.argv = ["beltctl.py", "1"]
import beltctl as B
import paho.mqtt.client as mqtt

torch.set_num_threads(2)   # 实测最优: 8 核机器上 threads=2 + imgsz=320 ≈ 53fps
                           # (threads=4/8 都 thrash; beltctl 默认的 4 线程在单核限制环境会 4s/张)
IMG_SZ = 320

V = 52.5
S_CONTACT = {1: 120.0, 2: 170.0, 3: 221.0}
L_SERVO = 0.28
S_TRIGGER = {b: S_CONTACT[b] - V * L_SERVO for b in (1, 2, 3)}
LEAD_S = 0.05
RECORD = "/home/rockets/laya-industrial-bench/sort_calibration.jsonl"
A0 = np.array([598.4, 96.8])
LANE = np.array([-0.707, 0.707])
ANCH = [(0.0, 0.0), (120.0, 139.6), (170.0, 230.1), (220.0, 346.1), (305.0, 545.0)]

def s_of(p):
    d = float((np.array(p) - A0) @ LANE)
    for (s1, d1), (s2, d2) in zip(ANCH, ANCH[1:]):
        if d <= d2:
            return s1 + (d - d1) * (s2 - s1) / (d2 - d1)
    return 305.0

def log(msg):
    print(f"[sorterC {time.strftime('%H:%M:%S')}] {msg}", flush=True)

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

MQ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sorterC")
MQ.connect("127.0.0.1", 1883)
MQ.loop_start()

def mqtt_pub(topic, payload):
    for _ in range(3):
        try:
            MQ.publish(topic, json.dumps(payload), qos=1).wait_for_publish(2)
            return
        except Exception as e:
            try:
                MQ.reconnect()
            except Exception:
                time.sleep(0.5)

def belt_cmd(action, speed=None):
    c = {"job_id": f"sorterC-{action}", "action": action}
    if speed is not None:
        c["speed_mps"] = speed
    mqtt_pub("sort/line1/conveyor/cmd", c)

def divert(b):
    mqtt_pub(f"sort/sorter{b}/cmd", {"job_id": f"C{b}{time.time():.0f}", "action": "divert"})

# ---------- 检测 ----------
def detect(f):
    """-> [(s, p, conf)]"""
    if f is None:
        return []
    r = B.Y(f, conf=0.10, iou=0.5, device='cpu', verbose=False, imgsz=IMG_SZ)[0]
    if r.boxes is None:
        return []
    out = []
    for i, xy in enumerate(r.boxes.xyxy):
        x1, y1, x2, y2 = [int(v) for v in xy]
        p = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        out.append((s_of(p), p, float(r.boxes.conf[i])))
    return out

# ---------- 静止物体黑名单 ----------
blacklist = []          # (x, y, s, ts)
static_cand = {}        # cell -> (first_t, count, s, p)

def is_blacklisted(p, tol=10):
    return any(abs(p[0] - x) < tol and abs(p[1] - y) < tol for x, y, _, _ in blacklist)

def static_update(dets, now):
    """同位置框持续 >=1s 且 3 次以上 -> 拉黑 (仓里香蕉/黄标/栏杆)"""
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

def live_dets(dets, now, tracker):
    """剔除黑名单; 跟踪中再按预测轨迹门控"""
    out = []
    for s, p, c in dets:
        if is_blacklisted(p):
            continue
        out.append((s, p, c))
    if tracker.state in ("TRACKING", "FIRED") and tracker.t0 is not None:
        pred = tracker.s0 + V * (now - tracker.t0)
        gated = [x for x in out if abs(x[0] - pred) <= 15]
        if gated:
            return gated
    return out

# ---------- 追踪器 ----------
class Tracker:
    def __init__(self, bin_n):
        self.bin = bin_n
        self.state = "IDLE"
        self.cands = []
        self.t0 = None
        self.s0 = None
        self.last_match_t = None
        self.div_pre = None
        self.t_fire = None
        self.fire_err_ms = None

    def s_pred(self, now):
        return self.s0 + V * (now - self.t0)

    def fire_time(self):
        return self.t0 + (S_TRIGGER[self.bin] - self.s0) / V

    def feed(self, now, dets):
        """返回事件: None | 'verify' | 'dropped'
        注意: 本方法零阻塞 — 串口/MQTT 读取不得放进发射临界路径"""
        if self.state == "IDLE":
            for s, p, c in dets:
                if -8 <= s <= 150 and c >= 0.15:
                    self.cands.append((now, s))
            self.cands = [x for x in self.cands if now - x[0] < 0.8]
            if len(self.cands) >= 3:
                dt = self.cands[-1][0] - self.cands[0][0]
                ds = self.cands[-1][1] - self.cands[0][1]
                if dt > 0.35 and 0.4 * V * dt <= ds <= 1.6 * V * dt:
                    s0 = float(np.median([x[1] for x in self.cands]))
                    if s0 > S_TRIGGER[self.bin] - 30:
                        log(f"!! 香蕉 s0={s0:.0f} 已过触发区 -> 无法拦截, 让它走")
                        self.state = "DROPPED"
                        return "dropped"
                    self.t0 = now - (self.cands[-1][1] - s0) / V
                    self.s0 = s0
                    self.state = "TRACKING"
                    self.last_match_t = now
                    log(f"=== 锁定仓{self.bin}: s0={s0:.0f}mm, 触发线 s={S_TRIGGER[self.bin]:.0f}, "
                        f"预计 {self.fire_time()-now:.2f}s 后发射 ===")
            return None
        if self.state == "TRACKING":
            pred = self.s_pred(now)
            for s, p, c in dets:
                if abs(s - pred) <= 15:
                    self.last_match_t = now
                    break
            lost = self.last_match_t is None or now - self.last_match_t > 2.0
            if lost and pred > S_TRIGGER[self.bin] - 40:
                log(f"!! 跟踪丢失 (s_pred={pred:.0f}mm) -> 放弃发射, 香蕉将掉出口端")
                self.state = "DROPPED"
                return "dropped"
            dt_fire = self.fire_time() - now
            if dt_fire <= LEAD_S:
                divert(self.bin)          # 直接发, 不读串口!
                self.t_fire = now
                self.fire_err_ms = dt_fire * 1000.0
                self.state = "FIRED"
                log(f"仓{self.bin}: divert 已发 (偏差 {self.fire_err_ms:+.0f}ms)")
            return None
        if self.state == "FIRED":
            if self.s_pred(now) > S_CONTACT[self.bin] + 60:
                return "verify"
            return None
        return None

def verify(tr, now):
    # 推杆扫完整周期要 ~2-4s, 计数器随后才 +1: 轮询最多 6s
    div_post = tr.div_pre
    t_end = time.time() + 6.0
    while time.time() < t_end:
        st = cmd("state", 2).strip()
        div_post = div_count(st, tr.bin)
        if div_post is not None and tr.div_pre is not None and div_post > tr.div_pre:
            break
        time.sleep(0.8)
    fired = (div_post is not None and tr.div_pre is not None and div_post > tr.div_pre)
    # 未推中判据: 香蕉仍按带速前进 -> 预测位置附近还有运动框
    pred = tr.s_pred(time.time())
    on_belt = False
    for _ in range(2):
        f = B.one_frame()
        for s, p, c in detect(f):
            if abs(s - pred) <= 15 and c >= 0.15:
                on_belt = True
        time.sleep(0.3)
    ok = fired and not on_belt
    log(f"仓{tr.bin}: 推杆 {tr.div_pre}->{div_post}{'(已执行)' if fired else '(未执行!)'}, "
        f"带面预测位: {'有物体' if on_belt else '空'} => {'推入成功' if ok else '未推中'}")
    open(RECORD, "a").write(json.dumps(dict(
        ts=round(time.time(), 3), bin=tr.bin, action="C_run",
        s0=round(tr.s0, 1), target=S_TRIGGER[tr.bin],
        t_fire_ms=round(tr.fire_err_ms, 1),
        div=f"{tr.div_pre}->{div_post}",
        result="ok" if ok else "miss"), ensure_ascii=False) + "\n")
    return ok

def main():
    st = cmd("state", 2).strip()
    log(f"串口: {st}")
    if "conveyor=stopped" in st:
        belt_cmd("start", 0.0525)
        log("带子启动 0.0525 m/s")
        time.sleep(2.0)
        log("确认: " + cmd("state", 2).strip())
    else:
        log("带子已在运行, 接管")
    log(f"触发线: " + " ".join(f"仓{b}=s{S_TRIGGER[b]:.0f}" for b in (1, 2, 3)))

    cur = 1
    tr = Tracker(cur)
    stall_t0 = None
    next_state_t = 0.0
    while True:
        try:
            now = time.time()
            f = B.one_frame()
            if f is None:
                if stall_t0 is None:
                    stall_t0 = now
                if now - stall_t0 > 4.0:
                    log("流超时 4s -> 继续 (纯定时发射不依赖帧)")
                    stall_t0 = now
                time.sleep(0.1)
                # 跟踪中即使无帧也要按时发射
                if tr.state == "TRACKING" and tr.fire_time() - now <= LEAD_S:
                    tr.feed(now, [])
                continue
            stall_t0 = None
            dets = detect(f)
            static_update(dets, now)
            # 后台刷新推杆计数器 (避开发射临界时刻前 0.5s, 防串口阻塞拖延发射)
            if tr.state != "TRACKING" or tr.fire_time() - now > 0.6:
                if now >= next_state_t:
                    st = cmd("state", 1.5).strip()
                    tr.div_pre = div_count(st, tr.bin)
                    next_state_t = now + 1.5
            ev = tr.feed(now, live_dets(dets, now, tr))
            if ev in ("verify", "dropped"):
                ok = verify(tr, time.time()) if ev == "verify" else False
                if ev == "verify" and ok:
                    cur = cur % 3 + 1
                    log(f"完成 -> 下一根去 仓{cur}")
                else:
                    log(f"下一根仍去 仓{cur}")
                tr = Tracker(cur)
        except Exception as e:
            log(f"异常(继续): {repr(e)[:150]}")
            time.sleep(0.5)

if __name__ == "__main__":
    main()
