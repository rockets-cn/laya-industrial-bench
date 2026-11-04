"""MQTT 分拣控制网关 (laya 分类结果 -> MQTT 命令 -> 3 分拣器 + 1 传送带).

链路:
  语音回路 (loop_ui /run) --HTTP POST /dispatch--> 本网关 --MQTT--> 控制器
  视觉传感器 (vision_input.py 或外部设备) --MQTT sort/inbound/vision--> 本网关
  其他系统 (SCADA/手工) --MQTT sort/inbound/command--> 本网关

设备侧 MQTT 约定 (详见 docs/sort-mqtt.md, 模拟设备见 sim_devices.py):
  下行命令 (QoS1):
    sort/line1/conveyor/cmd     {"job_id","action":"start|stop|pause|resume","speed_mps"}
    sort/sorter{n}/cmd          {"job_id","action":"divert|reset","material","qty","trigger"}
  上行应答 (QoS1, 网关按 job_id 关联):
    sort/line1/conveyor/ack/{job_id}  {"action","result":"ok|busy|fault","note"}
    sort/sorter{n}/ack/{job_id}       同上
  遥测 (设备周期发布, QoS1):
    sort/line1/conveyor/telemetry  {"state":"stopped|running|paused|fault","ts"}
    sort/sorter{n}/telemetry       {"state":"ready|armed|busy|fault","ts"}
  运动事件 (设备状态迁移时发, QoS0; 推杆何时推出/光门何时触发):
    sort/line1/motion  {"sorter","state","via":"immediate|photo|virtual|
                        timeout|reset|done|next|cancel","job_id","ts"}
    网关维护时间线 GET /motion (保留最近 motion_keep 条) + relay sort/event/motion
  人工: sort/human/fault_clear 任意 payload -> 清除故障锁存

统计 (统计分发): 内存计数 + JSONL 台账 (stats/sort_ledger.jsonl, 每 job 一行)
+ sort/stats/summary (retain, 周期刷新: 批次/物料/分拣器/命令延迟/吞吐/故障).

安全降级 (对齐语音回路哲学): 命令超时重试后仍无应答 -> 故障锁存 -> 暂停传送带,
需人工清除 (HTTP POST /fault/clear 或 MQTT sort/human/fault_clear) 才继续下发.

Run: .venv/bin/python sort_mqtt.py          # broker tcp://127.0.0.1:1883, HTTP :8123
     SORT_MQTT_BROKER=tcp://10.0.0.5:1883 .venv/bin/python sort_mqtt.py
"""

import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("by", os.path.join(HERE, "bench_yard.py"))
by = importlib.util.module_from_spec(spec)
spec.loader.exec_module(by)

MATERIALS = by.MATERIALS
YARDS = by.YARDS  # [1, 2, 3] 与分拣器编号 1:1 对应 (1号料场 = 1号分拣器)

DEFAULT_CFG = {
    "broker": "tcp://127.0.0.1:1883",
    "client_id": "sort-gateway",
    "http_port": 8123,
    "sorter_count": 3,
    "conveyor_id": "line1",
    "conveyor_speed_mps": 1.0,
    "ack_timeout_s": 3.0,
    "ack_retries": 2,
    # divert 实际耗时 ≈ 0.9s/件 (固件 SERVO 拨出+件间间隔), 固定 3s 超时
    # 在 qty≥4 时会误判超时 -> 重发 -> 设备 busy -> 故障锁存/重复拨料.
    # 因此 divert 超时 = ack_timeout_s + divert_timeout_per_item_s × qty.
    "divert_timeout_per_item_s": 1.0,
    # busy-ack = 设备正在执行本 job: 绝不重发 (重发=重复拨料), 只延长等待.
    "busy_wait_s": 3.0,
    "busy_wait_max_ext": 3,
    # 拨料触发方式: immediate=收到命令即拨 (默认, 时序靠安装距离标定);
    # photo=设备等拨料器前侧光门被物品通过才拨 (时序与网络延迟解耦;
    # 固件侧 MCP23017 PB0/1/2 接光门走物理光门, 未接时走虚拟光门
    # (相机检测锚点+带速预测, 见固件 Track.h), 再无匹配才回退 immediate).
    "divert_trigger": "immediate",
    # photo 模式的额外超时预算: 相机->光门的物品行程 + 光门反应时间.
    "photo_margin_s": 3.0,
    # 运动事件时间线 (sort/line1/motion): 保留最近 N 条, GET /motion 供
    # "让示意图动起来"的动画还原实际情况 (推杆何时推出/光门何时触发).
    "motion_keep": 100,
    "stats_period_s": 5.0,
    "stop_conveyor_after_batch": True,
    "queue_max": 16,
    "ledger_path": os.path.join(HERE, "stats", "sort_ledger.jsonl"),
    "material_map": os.path.join(HERE, "config", "material_map.json"),
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


class GatewayStats:
    """内存计数 + JSONL 台账. 所有更新在 _lock 内 (HTTP 读 / MQTT 写 并发)."""

    def __init__(self, ledger_path):
        self._lock = threading.Lock()
        self.ledger_path = ledger_path
        self.started = time.time()
        self.batches = {"total": 0, "done": 0, "fault": 0, "reject": 0,
                         "queue_dropped": 0}
        self.items = {"dispatched": 0, "by_sorter": {}, "by_material": {}}
        self.cmd = {"sent": 0, "ack_ok": 0, "ack_timeout": 0, "retries": 0}
        self.vision = {"events": 0, "dispatched": 0, "unknown": 0, "low_conf": 0,
                       "ignored_grace": 0}
        self.lat_cmd_ack = deque(maxlen=500)  # 命令->应答 延迟 (s)
        self.lat_batch = deque(maxlen=500)    # 批次 E2E 延迟 (s)
        self._items_ts = deque()               # (ts, qty) 滚动 5min 吞吐窗口

    def job_finished(self, job):
        with self._lock:
            self.batches["total"] += 1
            self.batches[job["outcome"]] += 1
            self.lat_batch.append(job.get("e2e_s", 0.0))
            line = {"ts": _now(), "job_id": job["job_id"],
                    "source": job.get("source"), "path": job.get("path"),
                    "outcome": job["outcome"], "actions": job.get("actions"),
                    "fault": job.get("fault"),
                    "e2e_ms": round(job.get("e2e_s", 0.0) * 1000, 1)}
            d = os.path.dirname(self.ledger_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.ledger_path, "a") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def cmd_sent(self):
        with self._lock:
            self.cmd["sent"] += 1

    def cmd_ack(self, ok, latency_s, retried):
        with self._lock:
            self.cmd["ack_ok" if ok else "ack_timeout"] += 1
            if retried:
                self.cmd["retries"] += 1
            self.lat_cmd_ack.append(latency_s)

    def item_dispatched(self, sorter, material, qty):
        with self._lock:
            self.items["dispatched"] += qty
            k = str(sorter)
            self.items["by_sorter"][k] = self.items["by_sorter"].get(k, 0) + qty
            self.items["by_material"][material] = \
                self.items["by_material"].get(material, 0) + qty
            now = time.time()
            self._items_ts.append((now, qty))
            while self._items_ts and now - self._items_ts[0][0] > 300:
                self._items_ts.popleft()

    def vision_event(self, kind):
        with self._lock:
            self.vision[kind] += 1

    def _pct(self, vals, p):
        if not vals:
            return 0.0
        v = sorted(vals)
        return v[min(len(v) - 1, int(p * len(v)))] * 1000

    @staticmethod
    def _mean(vals):
        return (sum(vals) / len(vals)) * 1000 if vals else 0.0

    def summary(self):
        with self._lock:
            now = time.time()
            window = [q for ts, q in self._items_ts if now - ts <= 300]
            span = max(1.0, min(300.0, now - self.started))
            thr = (sum(window) / span * 60) if window else 0.0
            return {
                "ts": _now(),
                "uptime_s": round(now - self.started, 1),
                "batches": dict(self.batches),
                "items": {"dispatched": self.items["dispatched"],
                           "by_sorter": dict(self.items["by_sorter"]),
                           "by_material": dict(self.items["by_material"])},
                "cmd": dict(self.cmd),
                "vision": dict(self.vision),
                "latency_ms": {
                    "cmd_to_ack": {"mean": round(self._mean(self.lat_cmd_ack)),
                                    "p95": round(self._pct(self.lat_cmd_ack, .95)),
                                    "max": round(max(self.lat_cmd_ack) * 1000, 1)
                                             if self.lat_cmd_ack else 0},
                    "batch_e2e": {"mean": round(self._mean(self.lat_batch)),
                                   "max": round(max(self.lat_batch) * 1000, 1)
                                           if self.lat_batch else 0},
                },
                "throughput_items_per_min": round(thr, 1),
            }


class SortGateway:
    """MQTT 网关主体: 订阅设备遥测/应答 + 入站指令, 顺序执行批次, 维护统计."""

    def __init__(self, cfg=None):
        self.cfg = dict(DEFAULT_CFG)
        if cfg:
            self.cfg.update(cfg)
        self.http_port = int(os.environ.get("SORT_GATEWAY_PORT",
                                               self.cfg["http_port"]))
        broker = os.environ.get("SORT_MQTT_BROKER", self.cfg["broker"])
        m = re.match(r"tcp://([^:/]+):(\d+)$", broker)
        if m:
            self.broker_host, self.broker_port = m.group(1), int(m.group(2))
        else:
            host, _, port = broker.partition(":")
            self.broker_host, self.broker_port = host or "127.0.0.1", int(port or 1883)

        self.mat_map = _load_json(self.cfg["material_map"],
                                  {"mode": "fixed", "fixed": {},
                                   "confidence_min": 0.5})
        self.stats = GatewayStats(self.cfg["ledger_path"])
        self.client_id = self.cfg["client_id"] + "-" + str(os.getpid())
        self.fault = None                          # 故障锁存 dict
        self.fault_lock = threading.Lock()
        self.job_queue = deque(maxlen=int(self.cfg["queue_max"]))
        self.queue_lock = threading.Lock()
        self.active_job = None
        self.active_lock = threading.Lock()
        self.dev_state = {}                        # {"conveyor": ..., "sorter1": ...}
        self.conveyor_note = ""                    # 最近一次 conveyor 遥测 note (带健康监测原因)
        self._pending = {}                         # (kind, name, job_id) -> ack 槽位
        self._pending_lock = threading.Lock()
        self._rr_counter = {}                      # round_robin 每物料轮转计数
        self._rr_lock = threading.Lock()
        self._job_seq = 0
        # 运动事件时间线 (设备 sort/line1/motion -> 环形缓冲 + relay):
        # 动画还原"实际情况"用, 保留最近 motion_keep 条.
        self.motion = deque(maxlen=int(self.cfg.get("motion_keep", 100)))
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=self.client_id)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self._connected = threading.Event()

    # ---------- MQTT ----------
    def _topics(self):
        c = self.cfg["conveyor_id"]
        # 注意: paho-mqtt 2.x 要求 + / # 独占一级, "sort/sorter+/..." 是非法过滤器
        return [f"sort/{c}/conveyor/telemetry", f"sort/{c}/conveyor/ack/+",
                "sort/+/telemetry", "sort/+/ack/+",
                f"sort/{c}/motion",
                "sort/inbound/command", "sort/inbound/vision",
                "sort/human/fault_clear"]

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"[gateway] 连接失败 rc={reason_code}", flush=True)
            return
        self._connected.set()
        for t in self._topics():
            client.subscribe(t, qos=1)
        print(f"[gateway] 已连接 {self.broker_host}:{self.broker_port} "
              f"(订阅 {len(self._topics())} 主题)", flush=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected.clear()
        code = getattr(reason_code, "value", reason_code)
        print(f"[gateway] 断开 rc={code} (自动重连中)", flush=True)

    def _on_message(self, client, userdata, msg):
        t = msg.topic
        try:
            payload = json.loads(msg.payload.decode() or "{}")
        except Exception:
            return
        m = re.match(r"sort/([^/]+)/conveyor/(telemetry|ack)(?:/(.+))?$", t)
        if m:
            if m.group(2) == "telemetry":
                self.dev_state["conveyor"] = payload.get("state", "unknown")
                self.conveyor_note = payload.get("note") or ""
                if self.dev_state["conveyor"] == "fault":
                    self._latch_conveyor_fault()
            else:
                self._set_ack("conveyor", self.cfg["conveyor_id"],
                              m.group(3) or "", payload)
            return
        m = re.match(r"sort/(sorter\d+)/(telemetry|ack)(?:/(.+))?$", t)
        if m:
            if m.group(2) == "telemetry":
                self.dev_state[m.group(1)] = payload.get("state", "unknown")
            else:
                self._set_ack("sorter", m.group(1), m.group(3) or "", payload)
            return
        if t == "sort/inbound/command":
            self.enqueue_job(payload.get("actions") or [],
                              source=payload.get("source", "mqtt"),
                              path=payload.get("path"))
        elif t == "sort/inbound/vision":
            self._on_vision(payload)
        elif t == "sort/human/fault_clear":
            self.clear_fault("mqtt")
        else:
            m = re.match(r"sort/([^/]+)/motion$", t)
            if m and m.group(1) == self.cfg["conveyor_id"]:
                self._on_motion(payload)

    def _on_motion(self, payload):
        """设备运动事件 (状态迁移时间线): 记环形缓冲 + relay 到
        sort/event/motion (QoS0), 供动画/台账消费者还原实际情况.
        item/of = 逐件件号 (qty>1 时每件一条事件序列; 旧固件/不适用缺省 0)."""
        ev = {"ts": _now(),
              "sorter": payload.get("sorter"),
              "state": payload.get("state"),
              "via": payload.get("via"),
              "job_id": payload.get("job_id", ""),
              "item": payload.get("item", 0),
              "of": payload.get("of", 0),
              # 数据来源: sim 模拟器显式带 src=sim; 物理设备 (固件) 未带 → 记 device.
              # 页面按此区分「模拟/真实」数据源, 各自还原, 互不混入.
              "src": payload.get("src") or "device"}
        self.motion.append(ev)
        self._publish("sort/event/motion", ev)

    def _set_ack(self, kind, name, job_id, payload):
        with self._pending_lock:
            p = self._pending.get((kind, name, job_id))
        if p:
            p["result"] = payload.get("result", "fault")
            p["note"] = payload.get("note", "")
            p["event"].set()

    # ---------- 视觉入站 ----------
    def _on_vision(self, payload):
        self.stats.vision_event("events")
        # 启动宽限期 (2026-11-04 用户反馈: 每次重启传送带自动运行):
        # 网关启动后 N 秒内忽略视觉事件, 防止带面上"开电前就在"的物料
        # 立即触发自动分拣/起带. 宽限结束后新入带物料正常自动分拣.
        grace = float(self.cfg.get("vision_grace_s", 0.0))
        if grace > 0 and time.time() - self.stats.started < grace:
            self.stats.vision_event("ignored_grace")
            print(f"[gateway] 启动宽限期内忽略视觉事件: "
                  f"{payload.get('material')} frame={payload.get('frame_id')}",
                  flush=True)
            return
        material = payload.get("material")
        conf = float(payload.get("confidence", 0.0))
        if material not in MATERIALS:
            self.stats.vision_event("unknown")
            print(f"[gateway] 视觉未知物料 {material!r} "
                  f"(frame {payload.get('frame_id')})", flush=True)
            return
        if conf < float(self.mat_map.get("confidence_min", 0.5)):
            self.stats.vision_event("low_conf")
            return
        sorter = self._map_to_sorter(material)
        if sorter is None:
            self.stats.vision_event("unknown")
            return
        # 视觉坐标 (HL2 像素系 + 固件换算的带面位置 pos_mm) 随批次留痕:
        # 网关目前不消费, 但上游标定 (相机几何 -> 物品在带上的实际位置,
        # 见 docs/sort-mqtt.md "标定") 与料道末端对账需要这份原始数据.
        vision = {"material": material, "confidence": conf}
        if payload.get("frame_id") is not None:
            vision["frame_id"] = payload.get("frame_id")
        for k in ("x", "y", "w", "h", "pos_mm"):
            v = payload.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vision[k] = v
        job = self.enqueue_job(
            [{"yard": sorter, "material": material, "qty": 1}],
            source="vision", path="VISION", extra=vision)
        if job:
            self.stats.vision_event("dispatched")

    def _map_to_sorter(self, material):
        n = int(self.cfg["sorter_count"])
        mode = self.mat_map.get("mode", "fixed")
        if mode == "round_robin":
            with self._rr_lock:
                i = self._rr_counter.get(material, 0)
                self._rr_counter[material] = i + 1
            return (i % n) + 1
        s = self.mat_map.get("fixed", {}).get(material)
        if isinstance(s, int) and s in YARDS[:n]:
            return s
        return None

    # ---------- 作业队列 ----------
    def _new_job_id(self):
        with self._seq_lock:
            self._job_seq += 1
            return "j%d-%d" % (int(time.time() * 1000) % 10**8, self._job_seq)

    def enqueue_job(self, actions, source="http", path=None, extra=None):
        """白名单校验 + 入队. 返回 job dict (成功) 或 None (故障锁存/非法).

        extra: 入站附带的元数据 (如视觉事件的 x/y/w/h 坐标), 随批次事件/台账留痕.
        """
        with self.fault_lock:
            if self.fault:
                return None
        if not actions or not isinstance(actions, list):
            with self.stats._lock:
                self.stats.batches["queue_dropped"] += 1
            return None
        for a in actions:
            if (not isinstance(a, dict) or a.get("yard") not in YARDS
                    or a.get("material") not in MATERIALS
                    or not (isinstance(a.get("qty"), int) and 1 <= a["qty"] <= 99)):
                with self.stats._lock:
                    self.stats.batches["queue_dropped"] += 1
                return None
        job = {"job_id": self._new_job_id(), "actions": actions,
               "source": source, "path": path or "HTTP", "t0": time.time()}
        if extra:
            job["vision"] = extra
        with self.queue_lock:
            if len(self.job_queue) >= self.cfg["queue_max"]:
                with self.stats._lock:
                    self.stats.batches["queue_dropped"] += 1
                return None
            self.job_queue.append(job)
        return job

    # ---------- 执行 ----------
    def worker(self):
        while not self._stop.is_set():
            with self.queue_lock:
                job = (self.job_queue.popleft() if self.job_queue else None)
            if job is None:
                time.sleep(0.05)
                continue
            with self.active_lock:
                self.active_job = job
            try:
                self._run_job(job)
            except Exception as e:
                job["outcome"] = "fault"
                job["fault"] = {"device": "gateway",
                                "error": f"{e.__class__.__name__}: {e}"}
                job["e2e_s"] = time.time() - job["t0"]
                self._latch_fault(job)
                self.stats.job_finished(job)
                self._batch_event(job, "fault")
            finally:
                with self.active_lock:
                    self.active_job = None

    def _run_job(self, job):
        job["outcome"] = "done"
        self._batch_event(job, "accepted")
        # 1. 传送带: 按最近遥测状态转移到 running
        #    (stopped->start / paused->resume / fault->故障等人工 reset)
        cstate = self.dev_state.get("conveyor")
        if cstate == "fault":
            return self._fail(job, "conveyor", "conveyor fault (需人工 reset)")
        if cstate != "running":
            action = "resume" if cstate == "paused" else "start"
            ok, note = self._conveyor_cmd(job, action)
            if not ok:
                return self._fail(job, "conveyor",
                                   f"传送带未 running (state={cstate}): {note}")
        # 2. 分拣下发 (保序). 动作层逐条 (action = 1 件), 但指令层同 料场+物料
        #    合并回一条 divert 命令 (qty=N): 设备按一条命令逐件执行 (motion 事件
        #    带件号 item k/N), 同 job_id 连发多条命令会被设备 busy-ack 折叠,
        #    合并后才物理正确 (固件/sim 状态机一致).
        groups, order = {}, []
        for a in job["actions"]:
            k = (a["yard"], a["material"])
            if k in groups:
                groups[k]["qty"] += a["qty"]
            else:
                groups[k] = dict(a)
                order.append(k)
        for k in order:
            g = groups[k]
            ok, note = self._divert_cmd(job, g["yard"], g)
            if not ok:
                return self._fail(job, f"sorter{g['yard']}",
                                   f"divert 无应答/失败: {note}")
        for a in job["actions"]:
            self.stats.item_dispatched(a["yard"], a["material"], a["qty"])
            self._publish("sort/event/dispatch",
                           {"ts": _now(), "job_id": job["job_id"],
                            "sorter": a["yard"], "material": a["material"],
                            "qty": a["qty"]})
        # 3. 完成: 批间停带 (配置项), 停带失败不阻塞 done
        if self.cfg["stop_conveyor_after_batch"]:
            self._conveyor_cmd(job, "stop", required=False)
        job["e2e_s"] = time.time() - job["t0"]
        self.stats.job_finished(job)
        self._batch_event(job, "done")

    def _fail(self, job, device, error):
        job["outcome"] = "fault"
        job["fault"] = {"device": device, "error": error, "ts": _now()}
        job["e2e_s"] = time.time() - job["t0"]
        self._latch_fault(job)
        self.stats.job_finished(job)
        self._batch_event(job, "fault")
        # 故障安全: 尽力停带 (无应答也继续)
        self._conveyor_cmd(job, "pause", required=False)

    def _latch_fault(self, job):
        with self.fault_lock:
            self.fault = job.get("fault") or {"device": "unknown",
                                                "error": "unknown"}
            self.fault["job_id"] = job["job_id"]
        self._publish("sort/event/fault", self.fault, retain=True)
        print(f"[gateway] 故障锁存: {self.fault} — 需人工清除 "
              f"(/fault/clear 或 sort/human/fault_clear)", flush=True)

    def clear_fault(self, via):
        with self.fault_lock:
            had = self.fault
            self.fault = None
        # 尽力向设备下发 reset: 固件 BeltWatch 锁存 (打滑/卡滞) 后 conveyor 处于
        # fault 态, 需设备侧 reset 回 stopped 才能恢复下发 (幂等, 无应答不阻塞).
        try:
            self._conveyor_cmd({"job_id": "fault-clear"}, "reset", required=False)
        except Exception:
            pass
        self._publish("sort/event/cleared",
                       {"ts": _now(), "via": via, "had": had}, retain=True)
        print(f"[gateway] 故障已清除 (via={via})", flush=True)
        return had

    def _latch_conveyor_fault(self):
        """conveyor 遥测报 fault (固件 BeltWatch: 打滑/卡滞) -> 锁存网关故障.

        锁存期拒绝新批次. 在途批次不在此处置 (跨线程改 job 有双结算风险):
        带子已停, 其后续命令会自然超时/失败 -> 由 worker 自身流程记 fault.
        清除: sort/human/fault_clear 或 POST /fault/clear (清除时网关尽力向
        设备下发 reset)."""
        with self.fault_lock:
            if self.fault:
                return
            self.fault = {"device": "conveyor",
                          "error": self.conveyor_note or "conveyor fault",
                          "ts": _now(), "src": "conveyor telemetry"}
            print(f"[gateway] 传送带故障锁存: {self.fault['error']}", flush=True)

    # ---------- 命令 + 应答 ----------
    def _cmd_topic(self, kind, name):
        if kind == "conveyor":
            return f"sort/{self.cfg['conveyor_id']}/conveyor/cmd"
        return f"sort/{name}/cmd"

    def _wait_ack(self, kind, name, job_id, timeout):
        """等 job_id 的应答. 返回 (result, note): result=None 表示超时未收到."""
        with self._pending_lock:
            p = {"event": threading.Event(), "result": None, "note": ""}
            self._pending[(kind, name, job_id)] = p
        try:
            p["event"].wait(timeout)
            return p["result"], p["note"]
        finally:
            with self._pending_lock:
                self._pending.pop((kind, name, job_id), None)

    def _send_with_retry(self, kind, name, job_id, payload, timeout=None):
        """发布命令 + 等应答. 返回 (ok, note).

        - ok-ack  -> 成功
        - fault-ack -> 立即失败 (重发不会让设备恢复)
        - busy-ack -> 设备正在执行本 job: 绝不重发 (重发 = 重复拨料),
          只延长等待 (busy_wait_s, 最多 busy_wait_max_ext 次)
        - 完全无应答 (真超时) -> 重发, 最多 ack_retries 次
        """
        to = float(timeout if timeout is not None
                   else self.cfg["ack_timeout_s"])
        retries = int(self.cfg["ack_retries"])
        busy_wait = float(self.cfg.get("busy_wait_s", 3.0))
        max_ext = int(self.cfg.get("busy_wait_max_ext", 3))
        t_start = time.perf_counter()
        pubs = 0
        exts = 0
        republished = 0
        need_pub = True
        saw_busy = False
        note = ""
        while True:
            wait_s = to if need_pub else busy_wait
            if need_pub:
                self.client.publish(self._cmd_topic(kind, name),
                                    json.dumps(payload), qos=1)
                self.stats.cmd_sent()
                pubs += 1
            need_pub = False
            result, note = self._wait_ack(kind, name, job_id, wait_s)
            if result == "ok":
                self.stats.cmd_ack(True, time.perf_counter() - t_start,
                                   republished > 0)
                return True, note
            if result == "fault":
                self.stats.cmd_ack(False, time.perf_counter() - t_start,
                                   republished > 0)
                return False, note or "fault"
            if result == "busy":
                exts += 1
                if exts > max_ext:
                    self.stats.cmd_ack(False, time.perf_counter() - t_start,
                                       republished > 0)
                    return False, note or "busy 超出等待窗口"
                saw_busy = True
                continue          # 不重发, 继续等 busy_wait_s
            # result is None: 窗口内无应答
            if saw_busy:
                # 设备已声明正在执行本 job (busy-ack), 只是还没做完
                # -> 按延长处理, 绝不重发
                exts += 1
                if exts > max_ext:
                    self.stats.cmd_ack(False, time.perf_counter() - t_start,
                                       republished > 0)
                    return False, note or "busy 超出等待窗口"
                continue
            if pubs > retries:
                self.stats.cmd_ack(False, time.perf_counter() - t_start,
                                   republished > 0)
                return False, note or "ack_timeout"
            need_pub = True
            republished += 1

    def _conveyor_cmd(self, job, action, required=True):
        payload = {"job_id": job["job_id"], "action": action, "ts": _now()}
        if action in ("start", "resume"):
            payload["speed_mps"] = float(self.cfg["conveyor_speed_mps"])
        ok, note = self._send_with_retry(
            "conveyor", self.cfg["conveyor_id"], job["job_id"], payload)
        return ok, note

    def _divert_cmd(self, job, sorter, action):
        payload = {"job_id": job["job_id"], "action": "divert", "ts": _now(),
                   "material": action["material"], "qty": action["qty"]}
        # 固件一件拨料 ≈ 0.9s, ack 在 qty 件全拨完才发 -> 超时随 qty 缩放
        qty = int(action.get("qty", 1))
        to = (float(self.cfg["ack_timeout_s"])
              + float(self.cfg.get("divert_timeout_per_item_s", 1.0)) * qty)
        # 光门触发: 设备等到物品过光门才拨, 加上相机->光门行程预算
        if self.cfg.get("divert_trigger") == "photo":
            payload["trigger"] = "photo"
            to += float(self.cfg.get("photo_margin_s", 3.0))
        return self._send_with_retry("sorter", f"sorter{sorter}",
                                     job["job_id"], payload, timeout=to)

    def _batch_event(self, job, phase):
        self._publish("sort/event/batch",
                       {"ts": _now(), "job_id": job["job_id"], "phase": phase,
                        "source": job.get("source"),
                        "actions": job.get("actions"),
                        "vision": job.get("vision"),
                        "fault": job.get("fault")},
                       retain=(phase in ("fault", "done")))

    def _publish(self, topic, payload, retain=False):
        try:
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False),
                                 qos=1 if retain else 0, retain=retain)
        except Exception as e:
            print(f"[gateway] 发布失败 {topic}: {e}", flush=True)

    # ---------- 统计周期 ----------
    def stats_loop(self):
        while not self._stop.is_set():
            time.sleep(float(self.cfg["stats_period_s"]))
            s = self.stats.summary()
            s["fault"] = self.fault
            self._publish("sort/stats/summary", s, retain=True)

    # ---------- HTTP ----------
    def make_http_handler(self):
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; "
                                                  "charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok",
                                      "broker_connected": gw._connected.is_set(),
                                      "active_job": (gw.active_job or {}).get("job_id"),
                                      "queue": len(gw.job_queue),
                                      "fault": gw.fault,
                                      # 传送带实时态 (running/stopped/paused/fault): 数字孪生按实际带态显示
                                      "conveyor": gw.dev_state.get("conveyor", "unknown"),
                                      # 带健康监测原因 (固件 BeltWatch: belt ok / belt slip / belt jam)
                                      "conveyor_note": gw.conveyor_note})
                elif self.path == "/stats":
                    s = gw.stats.summary()
                    s["fault"] = gw.fault
                    self._send(200, s)
                elif self.path == "/motion":
                    # 运动事件时间线 (设备状态迁移: 推杆何时推出/光门何时触发):
                    # "让示意图动起来"的动画据此还原实际情况.
                    self._send(200, {"count": len(gw.motion),
                                      "motion": list(gw.motion)})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n).decode() or "{}")
                except Exception:
                    self._send(422, {"error": "bad json"})
                    return
                if self.path == "/dispatch":
                    if gw.fault:
                        self._send(409, {"error": "fault latched, clear first",
                                         "fault": gw.fault})
                        return
                    job = gw.enqueue_job(body.get("actions"),
                                         source=body.get("source", "http"),
                                         path=body.get("path"))
                    if not job:
                        self._send(422, {"error": "invalid or rejected actions"})
                        return
                    self._send(202, {"job_id": job["job_id"],
                                      "queued": len(gw.job_queue)})
                elif self.path == "/fault/clear":
                    had = gw.clear_fault("http")
                    self._send(200, {"cleared": had is not None, "had": had})
                else:
                    self._send(404, {"error": "not found"})

        return H

    def http_loop(self):
        srv = ThreadingHTTPServer(("127.0.0.1", self.http_port),
                                   self.make_http_handler())
        srv.serve_forever()

    # ---------- 生命周期 ----------
    def start(self):
        self.client.connect(self.broker_host, self.broker_port)
        self.client.loop_start()
        if not self._connected.wait(10):
            raise RuntimeError("MQTT 连接超时")
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.stats_loop, daemon=True).start()
        threading.Thread(target=self.http_loop, daemon=True).start()
        print(f"[gateway] 就绪: broker {self.broker_host}:{self.broker_port}  "
              f"HTTP 127.0.0.1:{self.http_port}  "
              f"分拣器 x{self.cfg['sorter_count']} + 传送带 {self.cfg['conveyor_id']}",
              flush=True)
        return self

    def stop(self):
        self._stop.set()
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        print("[gateway] 停止", flush=True)


def main():
    cfg_file = _load_json(os.path.join(HERE, "config", "sort_gateway.json"), {})
    gw = SortGateway(cfg_file or None)
    gw.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        gw.stop()


if __name__ == "__main__":
    main()
