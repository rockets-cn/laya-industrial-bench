#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_run.py — 推杆位置测量协议 (手动精确停带, 全程不推料)

一件对象, 三个精确停点: 物件前缘在推杆1/2/3 上游各 MARGIN mm 处停带 WINDOW_S 秒。
不改固件、不触发推杆:
  1. divert 直接经 MQTT 发固件 (绕过网关 /dispatch → 无 verified job → 无超时故障锁存)
  2. 物件前缘过 IR = T0 → P 锚点取 **MQTT ir_present 0→1 边沿遥测帧的 steps_total**
     (固件同一 loop tick 先即时发帧再 onIrEdge, stepsLive() 相同, 精确等价串口 T0 行,
     不依赖串口)。串口 `[ir] T0` 行仅作交叉校验 (串口死机时协议照常跑)
  2b. 误触发防护: IR 边沿后 HL2 视野必须出现 bbox 覆盖 IR 位置带的检测
      (IR_x=496px±带, 视野右缘幽灵件 x≥611 不在带内 → 拒识)。无 → 判误触发:
      reset 清挂起推 + 重新 divert, 最多 3 次, 超了 abort
  3. 停点 = P_T0 + d_N - MARGIN (d_1/2/3 = 105/158/211, MARGIN=10 → P 永远到不了 p_mm,
     pushTick 结构性不触发)
  4. 停命令时刻 = t_T0 + (停点偏移)/V_actual (V_actual 由遥测实测, 闭环恒速);
     先做一次距推杆1 PRE mm 的校准停, 实测命令时延漂移 L, 之后三个精确停都扣 L
  5. 每次停后读遥测 steps_total 回读 (带停后步数冻结) → 物件前缘相对推杆的精确偏移
  6. 最后一停 WINDOW_S 秒后发 reset (直发 MQTT: 清固件队列 + 推杆收回), 带子保持停止

命令 (直发 MQTT, 与网关同一通道、串行互斥, 无双控):
  stop/start/reset : sort/line1/conveyor/cmd {job_id, action, speed_mps?}
  divert           : sort/sorter3/cmd       {job_id, action:"divert", material, qty}
回读:
  telemetry        : sort/line1/conveyor/telemetry {state,speed_mps,steps_total,ir_present}
                     (500ms 周期 + IR 沿即时)
  ack              : sort/line1/conveyor/ack/{job}, sort/sorter3/ack/{job}
  motion           : sort/line1/motion (出现 via=ir_due busy = 推杆动作了 → 异常中止)
  串口             : /dev/ttyACM0 只读 (O_RDONLY, 不碰 DTR/RTS), 抓 [ir]/[push]/fault 行

用法:
  .venv/bin/python tools/measure_run.py              # 完整测量
  .venv/bin/python tools/measure_run.py --preflight  # 只读预检 (连 MQTT+串口+测速, 不发任何命令)
延长窗口: 某停点测量窗口结束时, 若存在 /tmp/measure_hold 文件 → 带子不起, 等文件被删
          (最多等 120s)。助手负责 touch/rm。
"""
import json
import os
import re
import sys
import time
import errno
import queue
import threading

import paho.mqtt.client as mqtt

BROKER = "127.0.0.1"
PORT = 1883
SPM = 69.1                    # steps/mm (固件 STEPS_PER_MM)
V_DESIGN = 52.5               # mm/s (起带恒 0.0525 m/s)
D1, D2, D3 = 105.0, 158.0, 211.0   # 固件 config.h 标定值 (11-20 尺量: IR→1=105, 间距 53/53)
DD = (D1, D2, D3)
BIN = 3                       # divert 目标仓 → armed 推 = T3 (最后才可能"撞"到 armed 点)
MARGIN = 10.0                 # mm 精确停安全裕量
PRE = 30.0                    # mm 校准停 A 提前量
L_EST = 3.0                   # mm 校准停初始时延估计
WINDOW_S = 10.0               # 每推杆测量窗口(用户指定 10s)
TRIG_MM = 60.0                # 位置触发提前量: P 到达 p_mm−TRIG_MM 即发 stop
                              # (500ms 遥测帧粒度+停止延迟 → 实际落在推杆上游 ~20-40mm, 结构上够不到 p_mm)
T0_TIMEOUT_S = 180.0
HOLD_FILE = "/tmp/measure_hold"
LOG = "/tmp/measure_run.log"
SUMMARY = "/tmp/measure_summary.json"
PX_PER_MM = 4.0                 # HL2 帧标定 (RAW_DUMP 实测, 640px=160mm)
IR_MM_FROM_EDGE = 36.0          # IR 距视野右缘(上游入口) mm
IR_X_PX = 640 - IR_MM_FROM_EDGE * PX_PER_MM   # ≈496px: IR 在帧里的位置
BAND_LO, BAND_HI = IR_X_PX - 120, IR_X_PX + 45  # 有效 T0 的 bbox 覆盖带
VCONF_S = 1.2                   # IR 边沿后等 HL2 确认窗口 (推杆 4.3s 才到, 时间富余)
VCONF_PRE = 0.5                 # 边沿前允许的检测 (物件接近期)
MAX_FALSE_T0 = 3                # 误触发自愈次数上限

logf = open(LOG, "w", buffering=1)
def log(msg):
    line = time.strftime("%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d} {msg}"
    print(line, flush=True)
    logf.write(line + "\n")

T0_RE = re.compile(r"\[ir\] T0 -> (\d) 号仓 P=(\d+)mm p_mm=(\d+)mm")

class Abort(Exception):
    pass

class Runner:
    def __init__(self, job_prefix):
        self.job = lambda s: f"{job_prefix}-{s}"
        self.mqttc = mqtt.Client(client_id=f"measure-{job_prefix}", clean_session=True)
        self._conn = False
        self.tele = []          # (mono, steps, state, ir_present)
        self.acks = {}          # job_id -> (mono, doc)
        self.push_events = []   # (mono, doc)
        self.vision = []        # (mono, material, bbox)
        self.fault_at = None
        self.push_fired_at = None
        self.divert_queued_at = None
        self.ir_edge = None     # (mono, steps_total) — ir_present 0→1 边沿帧 (T0 锚点)
        self._prev_ir = None
        self.false_t0_count = 0
        self.t0_mono = None
        self.t0_wall = None
        self.P_T0 = None
        self.p_mm = None
        self.t0_serial = None   # 串口 T0 行 (交叉校验用)
        self.serial_bytes = 0
        self._serial_ok = False
        self.serial_q = queue.Queue()
        self._stop = threading.Event()

    # ---------- MQTT ----------
    def connect(self):
        if self._conn:
            return
        self.mqttc.on_message = self._on_message
        self.mqttc.connect(BROKER, PORT, 60)
        self.mqttc.loop_start()
        for t in ("sort/line1/conveyor/telemetry",
                  "sort/line1/motion",
                  "sort/line1/conveyor/ack/#",
                  f"sort/sorter{BIN}/ack/#",
                  "sort/inbound/vision"):
            self.mqttc.subscribe(t, 1)
        self._conn = True
        deadline = time.monotonic() + 5
        while not self.tele and time.monotonic() < deadline:
            time.sleep(0.1)
        if not self.tele:
            raise Abort("遥测 5s 未到, MQTT/固件链路异常")
        log(f"MQTT 已连, 遥测正常 (最新 state={self.tele[-1][2]})")

    def _on_message(self, client, userdata, msg):
        t, p = msg.topic, msg.payload
        try:
            doc = json.loads(p)
        except Exception:
            doc = {}
        mono = time.monotonic()
        if t == "sort/line1/conveyor/telemetry":
            st = doc.get("state")
            ir = doc.get("ir_present")
            self.tele.append((mono, doc.get("steps_total"), st, ir))
            if len(self.tele) > 500:
                del self.tele[:250]
            if st == "fault" and self.fault_at is None:
                self.fault_at = mono
            # T0 锚点: ir_present 0→1 边沿帧 (测量全程不入队, 无 divert 前提)
            if (ir == 1 and self._prev_ir == 0 and self.ir_edge is None):
                self.ir_edge = (mono, doc.get("steps_total"))
            self._prev_ir = ir
        elif t == "sort/inbound/vision":
            self.vision.append((mono, doc.get("material"), doc.get("bbox")))
            if len(self.vision) > 200:
                del self.vision[:100]
        elif t == "sort/line1/motion":
            self.push_events.append((mono, doc))
            if doc.get("state") == "busy" and doc.get("via") == "ir_due":
                self.push_fired_at = mono
        elif t.startswith("sort/line1/conveyor/ack/") or t.startswith(f"sort/sorter{BIN}/ack/"):
            job = t.rsplit("/", 1)[-1]
            self.acks[job] = (mono, doc)

    def publish(self, topic, payload):
        self.mqttc.publish(topic, json.dumps(payload, ensure_ascii=False))

    def cmd(self, topic, action, job, speed=None, extra=None, timeout=10.0, retry_once=True, check=True):
        """发命令并等 ack (ack 按 job_id 隔离, 无串扰)。check=False 跳过中止检查 (收尾用)。"""
        payload = {"job_id": job, "action": action}
        if speed is not None:
            payload["speed_mps"] = speed
        if extra:
            payload.update(extra)
        if check:
            self.check_aborts()
        self.publish(topic, payload)
        log(f"CMD {action} job={job}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            a = self.acks.get(job)
            if check:
                self.check_aborts()
            if a:
                log(f"ACK {action} result={a[1].get('result')} note={a[1].get('note')}")
                return a[1]
            time.sleep(0.05)
        if retry_once:
            log(f"CMD {action} job={job} 无ack, 重发一次")
            self.publish(topic, payload)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                a = self.acks.get(job)
                if check:
                    self.check_aborts()
                if a:
                    log(f"ACK {action} result={a[1].get('result')} note={a[1].get('note')}")
                    return a[1]
                time.sleep(0.05)
        raise Abort(f"{action} job={job} {timeout*2:.0f}s 无ack")

    def check_aborts(self):
        if self.fault_at:
            raise Abort("固件 fault (遥测 state=fault)")
        if self.push_fired_at:
            raise Abort("推杆动作了 (motion via=ir_due) — 测量协议被破坏")
        # 带子停转监测: state=running 但 steps_total 冻结 >3s
        # (RMT 未重启 / 物理卡带 — 11-20 曾出现: reset 中断推杆后 beltHold.hold 卡死)
        tel = self.tele
        if len(tel) >= 4:
            last = tel[-1]
            if last[2] == "running" and last[1] is not None:
                i = len(tel) - 1
                while i > 0 and tel[i - 1][1] == last[1]:
                    i -= 1
                if tel[i][2] == "running" and last[0] - tel[i][0] > 3.0:
                    raise Abort(f"带子停转: state=running 但 steps_total 冻结 "
                                 f"{last[0] - tel[i][0]:.1f}s ({last[1]}) — 带子卡死或 RMT 未重启")

    # ---------- 串口 ----------
    def start_serial(self):
        def th():
            try:
                fd = os.open("/dev/ttyACM0", os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
                self._serial_ok = True
                buf = b""
                while not self._stop.is_set():
                    try:
                        b = os.read(fd, 512)
                    except OSError as e:
                        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            time.sleep(0.01)
                            continue
                        break
                    if not b:
                        time.sleep(0.01)
                        continue
                    self.serial_bytes += len(b)
                    buf += b
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        s = raw.decode("utf-8", "replace").strip()
                        if s:
                            self.serial_q.put((time.monotonic(), time.time(), s))
            except Exception as e:
                log(f"串口读异常: {e!r}")
        threading.Thread(target=th, daemon=True).start()
        time.sleep(0.3)

    def drain_serial(self):
        """消费串口队列并记日志; T0 行存入 self.t0_serial (交叉校验)。"""
        while True:
            try:
                mono, wall, s = self.serial_q.get(timeout=0.2)
            except queue.Empty:
                return
            log(f"SERIAL {s}")
            m = T0_RE.search(s)
            if m and self.t0_serial is None:
                self.t0_serial = (mono, wall, int(m.group(1)),
                                  float(m.group(2)), float(m.group(3)))

    # ---------- 状态 ----------
    def latest_tele(self):
        return self.tele[-1]

    def wait_state(self, want, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_aborts()
            if self.tele and self.tele[-1][2] == want:
                return True
            time.sleep(0.05)
        cur = self.tele[-1][2] if self.tele else "?"
        raise Abort(f"等待 state={want} 超时 (当前 {cur})")

    def wait_running(self):
        st = self.tele[-1][2] if self.tele else "?"
        if st == "running":
            log("带子已在运行")
            return
        if st == "fault":
            raise Abort("state=fault, 需先 POST /fault/clear 清故障再起带")
        if st in ("stopped", "paused"):
            log(f"state={st} → 先起带 (0.0525 m/s)")
            self.cmd("sort/line1/conveyor/cmd", "start", self.job("pre-start"),
                     speed=0.0525)
        self.wait_state("running", 15)

    def measure_speed(self, seconds=3.5):
        """遥测拟合 V_actual (mm/s)。只取本次调用之后的帧 —
        排除起带爬坡段 (RMT 冷启动加速 ~3s, 11-20 曾把 3.5s 爬坡均值当带速: 29.2 vs 52.5)。"""
        log(f"测速中 ({seconds:.1f}s, 遥测 500ms/帧)...")
        idx = len(self.tele)
        time.sleep(seconds)
        ts = [(m, s) for (m, s, st, ir) in self.tele[idx:] if s is not None]
        if len(ts) < 4:
            raise Abort("测速帧不足")
        (t0, s0), (t1, s1) = ts[0], ts[-1]
        if t1 - t0 < 1.0:
            raise Abort("测速时间跨度不足")
        v = (s1 - s0) / (t1 - t0) / SPM
        log(f"V_actual = {v:.3f} mm/s (设计 {V_DESIGN}, 偏差 {(v - V_DESIGN) / V_DESIGN * 100:.2f}%)")
        return v

    def _divert(self, tag):
        """divert bin3 angle_sensor ×1; 返回 ack。"""
        ack = self.cmd(f"sort/sorter{BIN}/cmd", "divert", self.job(f"div-{tag}"),
                       extra={"material": "angle_sensor", "qty": 1}, timeout=10)
        if ack.get("result") != "queued":
            raise Abort(f"divert 未入队: {ack}")
        self.divert_queued_at = time.monotonic()
        self.ir_edge = None
        self._prev_ir = None
        self.t0_serial = None

    def vision_confirms(self, edge_mono):
        """IR 边沿前后 HL2 是否有 bbox 覆盖 IR 位置带的检测 → (bool, mat, bbox)。"""
        lo = edge_mono - VCONF_PRE
        hi = edge_mono + VCONF_S
        for mono, mat, bbox in list(self.vision):
            if not (lo <= mono <= hi) or not bbox or len(bbox) < 4:
                continue
            x0, x1 = bbox[0], bbox[0] + bbox[2]
            if x0 <= BAND_HI and x1 >= BAND_LO:
                return True, mat, list(bbox)
        return False, None, None

    def wait_t0(self, timeout=T0_TIMEOUT_S):
        """等 T0: MQTT ir_present 0→1 边沿帧 + HL2 视觉确认 (误触发自愈)。"""
        log(f"等待 T0 (物件前缘过 IR, 最长 {timeout:.0f}s; 锚点=MQTT 边沿帧, 视觉确认防误触发)...")
        if self.serial_bytes == 0:
            log("注意: 串口 0 字节 — T0 锚点走 MQTT 边沿帧, 协议照常 (串口仅交叉校验)")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_aborts()
            self.drain_serial()
            e = self.ir_edge
            if e:
                mono_e, steps_e = e
                ok, mat, bbox = self.vision_confirms(mono_e)
                if ok:
                    self.ir_edge = None
                    self._prev_ir = None
                    P = (steps_e / SPM if steps_e is not None else None)
                    bin_no = BIN
                    pmm = P + DD[BIN - 1]
                    if self.t0_serial:
                        sm = self.t0_serial
                        d_t = abs(sm[0] - mono_e)
                        d_p = abs(sm[3] - P) if P is not None else float("nan")
                        log(f"T0 交叉校验: 串口 T0 vs MQTT 边沿 Δt={d_t*1000:.0f}ms ΔP={d_p:.2f}mm")
                        if d_p <= 5:
                            P = sm[3]   # 串口行用固件 SPM 算的 P, 优先
                    self.t0_mono, self.t0_wall = mono_e, time.time()
                    self.P_T0, self.p_mm = P, pmm
                    log(f"T0 (MQTT 边沿帧确认): P_T0={P:.0f}mm, p_mm={pmm:.0f}mm "
                        f"(绑定 {bin_no} 号仓, d={pmm - P:.0f}mm, 视觉确认 {mat} bbox={bbox})")
                    return mono_e, time.time(), bin_no, P, pmm
                if time.monotonic() > mono_e + VCONF_S:
                    # 误触发: IR 边沿但视野无覆盖带的物件 — 忽略该边沿, 继续等
                    # (测量全程不入队, 推杆未武装, 无需清挂起推/重新入队)
                    self.ir_edge = None
                    self._prev_ir = None
                    self.false_t0_count += 1
                    log(f"误触发 #{self.false_t0_count}/{MAX_FALSE_T0}: IR 边沿但 HL2 视野 "
                        f"{VCONF_PRE:.1f}s~{VCONF_S:.1f}s 内无覆盖带检测 — 忽略, 继续等待")
                    if self.false_t0_count > MAX_FALSE_T0:
                        raise Abort("IR 反复误触发 — 检查 IR 传感器/对射光路, 中止")
            time.sleep(0.05)
        raise Abort("T0 超时 — 物件是否已放入? 是否在带子上? (若物件在队列空档期过了 IR, 需重放)")

    def stop_at(self, t_send_mono, job):
        """到 t_send_mono 时刻发 stop。"""
        while time.monotonic() < t_send_mono:
            self.check_aborts()
            self.drain_serial()
            time.sleep(min(0.02, max(0.0, t_send_mono - time.monotonic())))
        self.cmd("sort/line1/conveyor/cmd", "stop", job, timeout=10)

    def stop_at_P(self, target_P, job, timeout=30.0):
        """位置触发: 轮询遥测, P ≥ target_P 即发 stop, 返回读回的 P_stop (mm)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_aborts()
            self.drain_serial()
            P = self.latest_tele()[1] / SPM
            if P >= target_P:
                self.cmd("sort/line1/conveyor/cmd", "stop", job, timeout=10)
                return self.readback_P()
            time.sleep(0.1)
        raise Abort(f"{timeout:.0f}s 内未到达 P≥{target_P:.0f}mm (物件未上带? 未过 IR?)")

    def readback_P(self, wait=1.4):
        """停后回读冻结步数 → P (mm)。"""
        time.sleep(wait)
        self.drain_serial()
        p1 = p2 = None
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            st = self.tele[-1]
            if st[2] == "stopped" and st[1] is not None:
                if p1 is None:
                    p1 = st
                else:
                    p2 = st
                    break
            time.sleep(0.1)
            self.check_aborts()
        if p2 is None:
            raise Abort("回读失败: 停后未收到 2 帧冻结遥测")
        dsteps = abs(p2[1] - p1[1])
        if dsteps > 20:
            raise Abort(f"回读异常: 带子仍在走 (Δsteps={dsteps})")
        P = p2[1] / SPM
        log(f"回读 P_stop = {P:.1f} mm (冻结确认, Δsteps={dsteps})")
        return P

    def window(self, which):
        log(f"=== {which} 测量窗口 {WINDOW_S:.0f}s 开始 ===")
        end = time.monotonic() + WINDOW_S
        while time.monotonic() < end:
            self.check_aborts()
            self.drain_serial()
            if os.path.exists(HOLD_FILE):
                log(f"{which} 窗口遇 hold 文件, 等解除 (最多120s)...")
                hdeadline = time.monotonic() + 120
                while os.path.exists(HOLD_FILE) and time.monotonic() < hdeadline:
                    self.check_aborts()
                    self.drain_serial()
                    time.sleep(0.5)
                end = time.monotonic() + WINDOW_S
                log(f"{which} hold 解除, 重开 {WINDOW_S:.0f}s 窗口")
            time.sleep(0.2)
        log(f"=== {which} 测量窗口结束 ===")
        self.check_aborts()

    # ---------- 主流程 ----------
    def run_full(self):
        self.connect()
        self.start_serial()
        # 1) 先清队列再等 T0 (测量全程不入队 — 推杆未武装 → 结构上不可能推杆动作)
        self.cmd("sort/line1/conveyor/cmd", "reset", self.job("pre-rst"), timeout=10)
        self.wait_state("stopped", 10)
        time.sleep(0.3)
        log("队列已清空 (推杆未武装) — 起带, 等待物件过 IR (T0)")
        self.wait_running()
        time.sleep(6.5)   # 起带几秒后测速(仅记录; 停止用位置触发, 不依赖速度换算)
        V = self.measure_speed()
        log(f"速度记录: V_actual={V:.2f} mm/s (设计 {V_DESIGN}; 仅记录, 不再作为中止条件)")

        # 2) 等 T0
        mono, wall, bin_no, P, pmm = self.wait_t0()
        d_expect = DD[bin_no - 1] if 1 <= bin_no <= 3 else None
        d_actual = pmm - P
        log(f"T0: P_T0={P:.0f}mm, p_mm={pmm:.0f}mm (绑定 {bin_no} 号仓, d={d_actual:.0f}mm)"
            + (f", 期望 {d_expect:.0f}mm" if d_expect is not None else ""))
        if d_expect is not None and abs(d_actual - d_expect) > 2:
            log(f"警告: d_actual={d_actual:.0f} ≠ config {d_expect:.0f} — 固件 config 可能漂移")
        T = {1: P + D1, 2: P + D2, 3: P + D3}
        for k in (1, 2, 3):
            log(f"推杆{k} p_mm (T{k}) = {T[k]:.1f} mm (P_T0 + {DD[k-1]:.0f}mm)")

        results = {}

        # 3) 三停 (位置触发: 轮询遥测, P 到达 T[k]-TRIG_MM 即发 stop;
        #    500ms 帧粒度+停止延迟 → 落在推杆上游 ~20-40mm, 结构上够不到 p_mm, 推杆不会动作)
        for k in (1, 2, 3):
            if k > 1:
                self.cmd("sort/line1/conveyor/cmd", "start", self.job(f"pre-start{k}"),
                         speed=0.0525, timeout=10)
                self.wait_state("running", 15)
            Pk = self.stop_at_P(T[k] - TRIG_MM, self.job(f"s{k}"))
            off = T[k] - Pk
            log(f"推杆{k}: 物件前缘停在推杆{k}上游 {off:.1f} mm "
                f"(P_stop={Pk:.1f}, p_mm={T[k]:.1f})")
            results[f"stop{k}"] = {"P_stop_mm": round(Pk, 1),
                                   "offset_from_pusher_mm": round(off, 1),
                                   "t_wall": time.strftime("%H:%M:%S")}
            self.window(f"推杆{k}")

        # 5) 收尾 reset (清队列 + 推杆收回), 带子保持停止
        self.cmd("sort/line1/conveyor/cmd", "reset", self.job("rst"), timeout=10)
        self.wait_state("stopped", 10)
        time.sleep(1.0)
        self.drain_serial()
        log(f"motion(推杆) 事件计数: {len(self.push_events)} (应为 0)")
        results["final"] = {"state": self.tele[-1][2],
                            "push_events": len(self.push_events)}

        out = {"t0_wall": time.strftime("%H:%M:%S", time.localtime(self.t0_wall)),
               "P_T0_mm": round(self.P_T0, 1),
               "p_mm_mm": round(self.p_mm, 1),
               "V_actual_mm_s": round(V, 3),
               "targets_mm": {f"T{k}": round(T[k], 1) for k in (1, 2, 3)},
               **results}
        with open(SUMMARY, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        log(f"DONE — 摘要已写 {SUMMARY}; 带子停止, 物件在推杆3位置待取")
        return out


def main():
    preflight = "--preflight" in sys.argv
    prefix = "mm0" if preflight else "mm2"
    r = Runner(prefix)
    try:
        r.connect()
        r.start_serial()
        time.sleep(2.0)
        r.drain_serial()
        st = r.latest_tele()[2]
        if preflight:
            V = r.measure_speed(3.0)
            ok = st == "running" and 51 < V < 54
            serial_note = "OK" if (r._serial_ok and r.serial_bytes > 0) else "SILENT"
            if serial_note == "SILENT":
                log(f"串口静默 ({r.serial_bytes}B) — 协议照常 (T0 锚点走 MQTT 边沿帧), "
                    f"交叉校验缺失")
            log(f"PREFLIGHT {'OK' if ok else 'FAIL'}: state={st}, V={V:.3f} mm/s, "
                f"串口={serial_note}")
            r._stop.set()
            sys.exit(0 if ok else 2)
        r.run_full()
        sys.exit(0)
    except Abort as e:
        log(f"ABORT: {e}")
        # 尽力收尾: 1) 先 stop (状态机归位; 若有推杆在途, 让它走完 —
        #    reset 直接打断推杆会使 beltHold.hold 卡死, 带子再也起不来 [11-20 教训])
        #    2) 等推杆收回  3) reset (清队列+收推杆), 带子保持停止
        try:
            r.cmd("sort/line1/conveyor/cmd", "stop", r.job("abstop"),
                  timeout=10, retry_once=False, check=False)
        except Exception:
            pass
        try:
            deadline = time.monotonic() + 6
            while (any(d.get("state") == "busy" for _, d in r.push_events[-2:])
                   and time.monotonic() < deadline):
                r.drain_serial()
                time.sleep(0.3)
        except Exception:
            pass
        try:
            r.cmd("sort/line1/conveyor/cmd", "reset", r.job("abrst"),
                  timeout=10, retry_once=False, check=False)
        except Exception as e2:
            log(f"收尾异常: {e2!r}")
        try:
            time.sleep(1.0)
            r.drain_serial()
            st = r.latest_tele()[2] if r.tele else "?"
            log(f"收尾后 state={st}")
        except Exception:
            pass
        try:
            with open(SUMMARY, "w") as f:
                json.dump({"aborted": str(e),
                           "state": r.latest_tele()[2] if r.tele else None,
                           "P_T0_mm": r.P_T0,
                           "t0_wall": r.t0_wall,
                           "push_events": len(r.push_events)},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        r._stop.set()
        sys.exit(3)
    except Exception as e:
        import traceback
        log(f"ERROR: {e!r}\n{traceback.format_exc()}")
        r._stop.set()
        sys.exit(4)


if __name__ == "__main__":
    main()
