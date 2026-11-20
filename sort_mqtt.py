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
    + 最近视觉识别 GET /vision (实际看到的: 数字孪生按实显示相机标签/物品物料)
  人工: sort/human/fault_clear 任意 payload -> 清除故障锁存

统计 (统计分发): 内存计数 + JSONL 台账 (stats/sort_ledger.jsonl, 每 job 一行)
+ sort/stats/summary (retain, 周期刷新: 批次/物料/分拣器/命令延迟/吞吐/故障).

安全降级 (对齐语音回路哲学): 命令超时重试后仍无应答 -> 故障锁存 -> 暂停传送带,
需人工清除 (HTTP POST /fault/clear 或 MQTT sort/human/fault_clear) 才继续下发.

Run: .venv/bin/python sort_mqtt.py          # broker tcp://127.0.0.1:1883, HTTP :8123
     SORT_MQTT_BROKER=tcp://10.0.0.5:1883 .venv/bin/python sort_mqtt.py
"""

import json
import math
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

# 指令路由台账 (v2: 选仓权 = 语音指令, 不是固定映射表):
# 相机识别到的物料按 /dispatch 下发的指令需求路由 — demand[material] = 指令序
# (FIFO) 的 {yard, remaining, job_id} 队列, 与固件 divert 命令队列逐件执行顺序
# 对应 (带面 FIFO: 第一件认到的角度传感器 → 第一条角度指令的仓). 相机在光门
# 上游, 视觉事件先于拨出发生; 需求条目的有效期按"带面走完全程"裕量取 TTL,
# 过期即丢弃 (不虚构路由). 映射表 (material_map) 仅保留给遗留
# vision_dispatch=True 自动分拣模式与"无指令时"的兜底标注.
DEMAND_TTL_S = 180.0

DEFAULT_CFG = {
    "broker": "tcp://127.0.0.1:1883",
    "client_id": "sort-gateway",
    "http_port": 8123,
    "sorter_count": 3,
    "conveyor_id": "line1",
    # 标定安全带速 52.5mm/s (默认兜底: 配置文件缺 key/读不到时也用此值, 绝不退回 1.0)
    "conveyor_speed_mps": 0.0525,
    "ack_timeout_s": 3.0,
    "ack_retries": 2,
    # 仓距 (2026-11-20 起运行时可调, 免编译/烧录): IR 触点 -> 推杆, [d1,d2,d3] mm.
    # 单一真源: 改这里 → 重启网关或 POST /config/apply → 固件落 NVS (掉电/重烧保持).
    # 当前定案值 2026-11-20 用户尺量: 等距 53/53mm.
    "bin_dist_mm": [105.0, 158.0, 211.0],
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
    # 视觉入站 sort/inbound/vision 是否自动 dispatch (选仓). 默认 False = 只记录
    # (喂数字孪生"真实侧"), 选仓权在 PC laya 模型 (经 /dispatch 下发); True = 旧行为
    # (HuskyLens 自动下发, 仅作兼容保留).
    "vision_dispatch": False,
    # photo 模式的额外超时预算: 相机->光门的物品行程 + 光门反应时间.
    "photo_margin_s": 3.0,
    # 下发模式 (2026-11-19 方案 B: 物料校验式下发):
    #   blind    = 语音指令直接下发盲拨 qty=N (旧行为, 设备按序推, 不验物料);
    #   verified = 语音指令 = 需求, 不盲拨; HL2 逐件核对物料, 确认是指令物料才
    #              发逐件 divert, 不匹配件直通带尾 (没指令不推, 与 2026-11-18
    #              语义一致); verified 下无需求不回退映射表, 超时(未见到匹配
    #              物料) -> 故障锁存. 逐件 divert 由 vision 事件驱动, 结构上
    #              不可能双下发 (语音 job 不盲拨).
    "dispatch_mode": "blind",
    # verified 模式作业超时: 连续 N s 无任何一件被相机核对确认 -> 故障锁存
    # (显式失败, 绝不盲推). 滑动窗口: 每推成一件截止时间顺延 (2026-11-20,
    # 固定窗口对大批量必误伤). 实机建议 >= 90s (密集上料/放料节奏).
    "verified_timeout_s": 90.0,
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
                         "cancelled": 0, "queue_dropped": 0}
        self.items = {"dispatched": 0, "by_sorter": {}, "by_material": {}}
        self.cmd = {"sent": 0, "ack_ok": 0, "ack_timeout": 0, "retries": 0}
        self.vision = {"events": 0, "dispatched": 0, "recorded": 0,
                       "unknown": 0, "low_conf": 0, "ignored_grace": 0,
                       "deduped": 0}
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
        self.demands = {}                       # material -> deque({yard, remaining, job_id, t})
        self.demands_lock = threading.Lock()
        # verified 作业 (dispatch_mode=verified, 2026-11-19 方案 B):
        # 语音指令 = 需求, 不盲拨; 相机逐件核对并推 (vision 逐件 divert 带
        # parent_job_id 指回这里), 进度由 _verified_progress 记账, 超时由
        # verified_loop 处理. {job_id: {job, remaining, pushed, deadline}}
        self.verified_jobs = {}
        self.verified_lock = threading.Lock()
        # 完成延时停带 (用户 2026-11-19: 完成任务后 10s 停传送带):
        # 作业完成时刻 + idle_stop_after_job_s; 新作业/手动起带清除;
        # verified_loop 到期且全闲时发 stop. None=无待停.
        self._idle_stop_at = None
        self.dev_state = {}                        # {"conveyor": ..., "sorter1": ...}
        self.conveyor_note = ""                    # 最近一次 conveyor 遥测 note (带健康监测原因)
        self.ir_present = False                    # 红外 T0 实时"物品在 IR"态 (固件每 500ms 上报 + 沿即时)
        self.ir_edge_count = 0                     # 红外 T0 上升沿累计 (物体过 IR 次数): 事件驱动, 绝不漏, 供孪生锁存
        # 仓距运行时可调 (2026-11-20): 设备侧实际值 (来自遥测 bin_dist) 与
        # config bin_dist_mm 对账; _dev_last_tele 用于检测设备重新上线 → 重推.
        self.bin_dist_device = None
        self._dev_last_tele = 0.0
        self._pending = {}                         # (kind, name, job_id) -> ack 槽位
        self._pending_lock = threading.Lock()
        self._rr_counter = {}                      # round_robin 每物料轮转计数
        self._rr_lock = threading.Lock()
        self._job_seq = 0
        self._vd_last = {}                      # 物料 -> 最近一次 vision 事件时刻 (存在性去重)
        # 运动事件时间线 (设备 sort/line1/motion -> 环形缓冲 + relay):
        # 动画还原"实际情况"用, 保留最近 motion_keep 条.
        self.motion = deque(maxlen=int(self.cfg.get("motion_keep", 100)))
        # 最近视觉识别事件 (HuskyLens 2 实际看到的, 白名单+置信度校验通过):
        # 数字孪生按"实际看到的"显示相机识别标签与物品物料, GET /vision
        self.vision_events = deque(maxlen=32)
        # 模型 vs 实际对照 (数字孪生 GET /jobs): 最近批次的
        # (laya 命令物料/仓/数量 vs edge101 执行成败 vs HuskyLens 实际看到)
        # 环形缓冲, 逐件对照 + mismatch 高亮. 选仓权在 PC laya, 这里只做对照.
        self.job_history = deque(maxlen=100)
        self.job_history_lock = threading.Lock()
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
                f"sort/{c}/config/ack/+",
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
                _new_ir = bool(payload.get("ir_present", False))
                if _new_ir and not self.ir_present:
                    self.ir_edge_count += 1   # 上升沿: 一件物体过 IR (事件驱动记录, 绝不漏)
                self.ir_present = _new_ir
                if isinstance(payload.get("bin_dist"), list) and len(payload["bin_dist"]) == 3:
                    self.bin_dist_device = [float(x) for x in payload["bin_dist"]]
                # 设备重新上线 (遥测中断 >10s 后恢复): 重推仓距, 保 json→设备一致
                _now_t = time.time()
                if self._dev_last_tele and _now_t - self._dev_last_tele > 10.0:
                    self._push_bin_dist("device-reconnect", wait_s=None)
                self._dev_last_tele = _now_t
                if self.dev_state["conveyor"] == "fault":
                    self._latch_conveyor_fault()
            else:
                self._set_ack("conveyor", self.cfg["conveyor_id"],
                              m.group(3) or "", payload)
            return
        m = re.match(r"sort/([^/]+)/config/ack/(.+)$", t)
        if m:
            self._set_ack("config", m.group(1), m.group(2), payload)
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
        # HuskyLens2 定制模型(旧固件, Data段10字节)不上报真实 conf:
        # SDK 按 V0.4 布局把 offset10-13 当 4B float 读, 实际是
        # name_length+名字前3字节 → 解出次正规负数, JSON 序列化后 ≈ -0.0。
        # 注意: Python 里 -0.0 >= 0 为 True, 必须用 copysign 区分符号。
        # conf 为负(含 -0.0)视为"无有效置信度", 跳过 confmin 过滤直接放行。
        # 日后升级 HuskyLens2 固件到 V0.4(14字节, 有真conf)后此分支自动失效。
        if math.copysign(1, conf) >= 0 and conf < float(self.mat_map.get("confidence_min", 0.5)):
            self.stats.vision_event("low_conf")
            return
        # 去向标注 (/vision 孪生显示用): 优先语音指令需求 (peek 不消耗),
        # 无需求回退映射表标注. 2026-11-18: 标注不再触发消耗 — 需求只在真正
        # 下发时消耗一次 (见下方 dispatch 分支), 且 vision_dispatch=False 时
        # 完全不碰需求台账 (视觉只记录, 修"没指令也自动推"后不再误耗需求).
        route = self._demand_route(material, consume=False)
        label_yard = route[0] if route else None
        if label_yard is None:
            label_yard = self._map_to_sorter(material)
        if label_yard is None:
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
        # 记账"实际看到的" (校验通过即记录, 记录在故障/队列检查之前:
        # 相机看到的事实不受故障锁存/队列满影响; 数字孪生 GET /vision
        # 按相机实际识别显示标签与物品物料)
        self.vision_events.append({
            "ts": _now(),
            "t": time.time(),
            "material": material,
            "confidence": conf,
            "yard": label_yard,
            "frame_id": vision.get("frame_id"),
            "pos_mm": vision.get("pos_mm"),
        })
        # 选仓权在 PC laya 模型 (经 sort/inbound/command / POST /dispatch 下发).
        # HuskyLens 视觉事件默认只记录 (喂孪生"真实侧"), 不自动下发; 只有
        # vision_dispatch=True 才按旧行为 dispatch (HL 决定仓).
        # 存在性去重: HL 对同一件在视野内每帧都报. 固定下发窗口 (旧
        # vision_dispatch_dedupe_s=3s) 短于单件视野停留时间 (带速 0.0525 m/s,
        # 视野纵深 ~300mm ≈ 5.7s) 时, 同一件被下发 2+ 次 -> 固件 24 件 FIFO
        # 幻影件净累积 -> 队列满故障 (2026-11-16 实测: 带子 running 期间
        # ~3s/条下发 vs ~9s/件 IR 消费, 9 分钟内两次队列满). 现改为:
        # 同一物料间隔 <= vision_dispatch_absence_s 视为"同一件持续在视野内"
        # 不下发; 间隔超过 (件出视野, 新件入) 才下发一次. 一件 = 一条 divert.
        # 2026-11-19 方案 B: verified 模式 (dispatch_mode=verified) 同样逐件
        # vision 下发 — 语音指令是需求, 相机核对物料后推; verified 下无需求
        # 不回退映射表 (没指令不推, 件直通带尾, 2026-11-18 语义).
        if self.cfg.get("vision_dispatch", False) \
                or self.cfg.get("dispatch_mode") == "verified":
            now = time.time()
            absence_s = float(self.cfg.get("vision_dispatch_absence_s", 0.8))
            continuously_in_view = (now - self._vd_last.get(material, 0.0)) <= absence_s
            self._vd_last[material] = now
            if not continuously_in_view:
                # 新件入视野: 仅在此处消耗 1 件需求并下发 (一件 = 一条 divert).
                # 需求优先路由; 无需求: 旧 HL 自动分拣模式回退映射表,
                # verified 模式 = 无下发, 该件直通带尾.
                route = self._demand_route(material, consume=True)
                if route is None:
                    if self.cfg.get("dispatch_mode") == "verified":
                        self.stats.vision_event("recorded")
                        return
                    yard, parent_id = self._map_to_sorter(material), None
                else:
                    yard, parent_id = route
                job = self.enqueue_job(
                    [{"yard": yard, "material": material, "qty": 1}],
                    source="vision", path="VISION", extra=vision)
                if job:
                    if parent_id:
                        # verified 进度归属: 该逐件 divert 完成后回写父语音 job
                        job["parent_job_id"] = parent_id
                    self.stats.vision_event("dispatched")
                else:
                    self.stats.vision_event("recorded")
            else:
                self.stats.vision_event("deduped")
        else:
            self.stats.vision_event("recorded")

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

    # ---------- 指令路由台账 (v2: 语音指令指定路由) ----------
    def _demand_add(self, yard, material, qty, job_id):
        """divert 命令成功下发后登记需求: 该物料接下来 qty 件按此仓路由."""
        with self.demands_lock:
            self.demands.setdefault(material, deque()).append(
                {"yard": yard, "remaining": qty, "job_id": job_id,
                 "t": time.time()})

    def _demands_snapshot(self):
        """需求台账快照 (/health 暴露): 逐条 {material, yard, remaining, job_id},
        仅 remaining>0 — 页面「仓库分拣下发」列表按此实时减行
        (2026-11-20: 每实推一件 consume 1, 对应物料行数 -1)."""
        with self.demands_lock:
            return [{"material": mat, "yard": e["yard"],
                     "remaining": e["remaining"], "job_id": e["job_id"]}
                    for mat, dq in sorted(self.demands.items())
                    for e in dq if e["remaining"] > 0]

    def _demand_drop_job(self, job_id):
        """丢弃某 job 的未消费需求 (job 失败/撤销: 不再按它的仓路由)."""
        with self.demands_lock:
            for mat in list(self.demands.keys()):
                dq = self.demands[mat]
                for e in list(dq):
                    if e["job_id"] == job_id:
                        dq.remove(e)
                if not dq:
                    del self.demands[mat]

    def _demand_route(self, material, consume=True):
        """按指令需求路由: 返回 (yard, job_id) — 该物料下一件应去的仓 (FIFO) +
        提出该需求的指令 job_id (verified 进度记账用, 见 2026-11-19 方案 B).
        无需求/全部过期 → None (调用方回退映射表).
        consume=True: 消耗 1 件 (真正下发时用, 一件=一次消耗);
        consume=False: 只 peek 不消耗 (/vision 去向标注用, 不触发下发).
        2026-11-18: 旧版每个视觉事件都消耗 -> 一件在视野停留 5.7s(上百帧)
        会把整条需求耗光, 后续件丢失路由; 现改为仅在实际下发时消耗.
        2026-11-19 (方案 B): 返回值改为 (yard, job_id) 元组, 逐件 vision job
        可归属父语音 job (verified 进度); 调用方需解包."""
        with self.demands_lock:
            dq = self.demands.get(material)
            if not dq:
                return None
            now = time.time()
            while dq and (now - dq[0]["t"] > DEMAND_TTL_S
                          or dq[0]["remaining"] <= 0):
                dq.popleft()
            if dq and dq[0]["remaining"] > 0:
                if consume:
                    dq[0]["remaining"] -= 1
                return dq[0]["yard"], dq[0]["job_id"]
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
                self._record_job(job)
                if job.get("parent_job_id"):
                    self._verified_progress(job["parent_job_id"], "fault")
            finally:
                with self.active_lock:
                    self.active_job = None

    def _run_job(self, job):
        # 新作业开始: 撤销任何待停 (命令一到就起带, 不再停)
        self._idle_stop_at = None
        job["outcome"] = "done"
        self._batch_event(job, "accepted")
        # 指令路由台账登记时机 = job 开始 (命令已受理即生效, 早于物理执行;
        # 失败 job 由 _fail 作废). source=vision 的 job 是视觉事件自身路由的
        # 结果 (需求已在 _on_vision 消耗), 不回填 — 否则台账自我膨胀且永不耗尽.
        if job.get("source") != "vision":
            for a in job["actions"]:
                self._demand_add(a["yard"], a["material"], a["qty"],
                                 job["job_id"])
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
        # 1.5 verified 模式 (2026-11-19 方案 B): 语音指令 = 需求, 不盲拨.
        #     需求已在 job 开头登记; 这里只做起带 + 转"待核对". 物理逐件
        #     divert 由 vision 事件逐件下发 (_on_vision, 逐件 job 带
        #     parent_job_id), 进度由 _verified_progress 记账, 超时由
        #     verified_loop 处理. 核对期间带子不自动停 (料要持续上带直到
        #     需求耗尽); 任务完成后 idle_stop_after_job_s 秒自动停带.
        if self.cfg.get("dispatch_mode") == "verified" \
                and job.get("source") != "vision":
            remaining = sum(int(a["qty"]) for a in job["actions"])
            vtimeout = float(self.cfg.get("verified_timeout_s", 90.0))
            job["outcome"] = "pending"
            job["verified"] = True
            job["divert_results"] = []
            with self.verified_lock:
                self.verified_jobs[job["job_id"]] = {
                    "job": job,
                    "remaining": remaining,
                    "pushed": 0,
                    "deadline": time.time() + vtimeout,
                }
            self._batch_event(job, "pending")
            print(f"[gateway] verified job {job['job_id']} 待核对: "
                  f"{job['actions']} (超时 {vtimeout:.0f}s)", flush=True)
            return
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
        job["divert_results"] = []
        for k in order:
            g = groups[k]
            ok, note = self._divert_cmd(job, g["yard"], g)
            job["divert_results"].append({"yard": g["yard"],
                                          "material": g["material"],
                                          "qty": g["qty"], "ok": ok, "note": note})
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
        self._record_job(job)
        if job.get("parent_job_id"):
            # verified 进度: 逐件 vision divert 完成 -> 父语音 job pushed+1
            # (父作业耗尽才算"任务完成", 停带时机由 _verified_progress 安排)
            self._verified_progress(job["parent_job_id"], "done")
        else:
            self._schedule_idle_stop()

    def _schedule_idle_stop(self):
        """任务完成后开始 N 秒停带倒计时 (idle_stop_after_job_s; 0=禁用)."""
        delay = float(self.cfg.get("idle_stop_after_job_s", 0) or 0)
        if delay <= 0:
            return
        self._idle_stop_at = time.time() + delay
        print(f"[gateway] 任务完成, {delay:.0f}s 内无新作业/起带则自动停带",
              flush=True)

    def _fail(self, job, device, error):
        job["outcome"] = "fault"
        job["fault"] = {"device": device, "error": error, "ts": _now()}
        job["e2e_s"] = time.time() - job["t0"]
        self._demand_drop_job(job["job_id"])  # 失败 job 的需求作废, 不再按它路由
        self._latch_fault(job)
        self.stats.job_finished(job)
        self._batch_event(job, "fault")
        self._record_job(job)
        # 故障安全: 尽力停带 (无应答也继续)
        self._conveyor_cmd(job, "pause", required=False)
        if job.get("parent_job_id"):
            # verified 进度: 逐件 divert 失败 -> 父语音 job 一并 fault
            # (机器故障已由逐件 job 锁存, 父 job 只记账不重复锁存)
            self._verified_progress(job["parent_job_id"], "fault")

    def _record_job(self, job):
        """模型 vs 实际对照: 批次完成后记录 (laya 命令 / edge101 执行成败 /
        逐仓 divert 结果), 数字孪生 GET /jobs 逐项对照 + mismatch 高亮.
        HuskyLens "实际看到" 由 /jobs 端点按时间窗就近关联 (见 do_GET /jobs)."""
        rec = {
            "job_id": job["job_id"],
            "ts": _now(),
            "t0": job.get("t0"),
            "t_done": time.time(),
            "source": job.get("source"),
            "actions": job.get("actions"),
            "pushed": job.get("pushed"),        # verified: 已核对推送件数
            "remaining": job.get("remaining"),  # verified: 未核对件数
            "outcome": job.get("outcome"),
            "fault": job.get("fault"),
            "e2e_ms": round(job.get("e2e_s", 0.0) * 1000, 1),
            "divert_results": job.get("divert_results"),
            # verified 逐件子 job 指回父语音 job (2026-11-20: 页面按此把
            # motion 物理推完事件归并到语音单, 「已全部推完」只在真推完后显示)
            "parent_job_id": job.get("parent_job_id"),
        }
        with self.job_history_lock:
            self.job_history.append(rec)

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
            if result in ("ok", "queued"):
                # ok     = 设备已完成 (sim 全推完才回 ok)
                # queued = 固件 divert 已入队即回, 物理推杆异步进行, 最后件
                #          推完固件另发一条 ok (网关 fire-and-forget, 不阻塞等).
                # 两者都算"命令已被接受" -> 成功, 绝不重发 (重发 = 重复入队).
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

    # ---------- 仓距下发 (2026-11-20 运行时可调) ----------
    def _push_bin_dist(self, reason="startup", wait_s=None):
        """把 config 的 bin_dist_mm 推给固件 (sort/line1/config/cmd set_bin_dist).

        固件校验 (0<d1<d2<d3<400) → 写 RAM + 落 NVS → ACK. wait_s=None 时
        fire-and-forget (启动/设备重连场景: 设备可能还没上线, 不等 ack);
        /config/apply 用 wait_s=ack_timeout_s 同步等应答. 返回 result
        (ok/fault/None=未等/None=超时).
        """
        vals = self.cfg.get("bin_dist_mm") or []
        if len(vals) != 3:
            print(f"[gateway] bin_dist_mm 缺/非法 {vals!r}, 不推送", flush=True)
            return None
        try:
            vals = [float(v) for v in vals]
        except Exception:
            print(f"[gateway] bin_dist_mm 非数值 {vals!r}, 不推送", flush=True)
            return None
        job_id = self._new_job_id()
        payload = {"job_id": job_id, "action": "set_bin_dist",
                   "bin_dist": vals, "ts": _now()}
        self._publish(f"sort/{self.cfg['conveyor_id']}/config/cmd", payload)
        if wait_s is None:
            print(f"[gateway] bin_dist 已推送 {vals} ({reason}, fire-and-forget)",
                  flush=True)
            return "sent"
        result, note = self._wait_ack("config", self.cfg["conveyor_id"],
                                      job_id, float(wait_s))
        tag = "ok" if result == "ok" else ("timeout" if result is None else result)
        print(f"[gateway] bin_dist 推送 {tag}: {vals} ({reason}) note={note}",
              flush=True)
        if result is not None and result != "ok":
            return f"{result}: {note}"   # fault 时带上设备原因, 供 HTTP 400 回显
        return result

    def _push_bin_dist_with(self, vals, reason="http", wait_s=None):
        """_push_bin_dist 的定值版: 推送给定的 [d1,d2,d3] (而非 config 值).
        供 /config/apply 请求体传值用; 返回 ok/"sent"/"fault: 原因"/None(超时)."""
        try:
            vals = [float(v) for v in vals]
        except Exception:
            return "bin_dist 需 [d1,d2,d3] 三个值"
        if len(vals) != 3:
            return "bin_dist 需 [d1,d2,d3] 三个值"
        job_id = self._new_job_id()
        payload = {"job_id": job_id, "action": "set_bin_dist",
                   "bin_dist": vals, "ts": _now()}
        self._publish(f"sort/{self.cfg['conveyor_id']}/config/cmd", payload)
        if wait_s is None:
            print(f"[gateway] bin_dist 已推送 {vals} ({reason}, fire-and-forget)",
                  flush=True)
            return "sent"
        result, note = self._wait_ack("config", self.cfg["conveyor_id"],
                                      job_id, float(wait_s))
        tag = "ok" if result == "ok" else ("timeout" if result is None else result)
        print(f"[gateway] bin_dist 推送 {tag}: {vals} ({reason}) note={note}",
              flush=True)
        if result is not None and result != "ok":
            return f"{result}: {note}"   # fault 时带上设备原因, 供 HTTP 400 回显
        return result

    # ---------- verified (2026-11-19 方案 B: 物料校验式下发) ----------
    def _verified_progress(self, job_id, outcome):
        """verified 作业进度记账: 逐件 vision divert done -> pushed+1 /
        remaining-1, 耗尽即父作业完成; 逐件失败 -> 父作业 fault (机器故障
        已由逐件 job 锁存, 这里只记账 + 作废剩余需求, 不重复锁存)."""
        with self.verified_lock:
            vj = self.verified_jobs.get(job_id)
        if not vj:
            return
        job = vj["job"]
        if outcome == "done":
            with self.verified_lock:
                vj["pushed"] += 1
                vj["remaining"] -= 1
                # 滑动超时 (2026-11-20): 每推成一件, 截止时间顺延一个窗口 —
                # 固定 90s 对大批量必误伤 (实测 99 件 @ ~3.1s/件需 5 分钟,
                # 推到 29 件被超时锁存); 语义改为「连续 N s 零进展才故障」,
                # 有进展的作业永不超时, 小单行为不变.
                vj["deadline"] = (time.time()
                                  + float(self.cfg.get("verified_timeout_s",
                                                       90.0)))
                finished = vj["remaining"] <= 0
            if not finished:
                self._publish("sort/event/verified",
                               {"ts": _now(), "job_id": job_id,
                                "pushed": vj["pushed"],
                                "remaining": vj["remaining"]})
                return
            job["outcome"] = "done"
            job["e2e_s"] = time.time() - job["t0"]
            job["pushed"] = vj["pushed"]
            job["remaining"] = 0
            with self.verified_lock:
                self.verified_jobs.pop(job_id, None)
            self._batch_event(job, "done")
            self.stats.job_finished(job)
            self._record_job(job)
            self._publish("sort/event/verified",
                           {"ts": _now(), "job_id": job_id, "phase": "done",
                            "pushed": vj["pushed"]}, retain=True)
            # 需求全部核对完成 = 任务完成: 开始停带倒计时
            self._schedule_idle_stop()
        else:
            job["outcome"] = "fault"
            if not job.get("fault"):
                job["fault"] = {"device": "sorter",
                                 "error": f"verified 逐件下发失败 ({outcome})",
                                 "ts": _now()}
            job["e2e_s"] = time.time() - job["t0"]
            job["pushed"] = vj["pushed"]
            job["remaining"] = vj["remaining"]
            with self.verified_lock:
                self.verified_jobs.pop(job_id, None)
            self._demand_drop_job(job_id)
            self._batch_event(job, "fault")
            self.stats.job_finished(job)
            self._record_job(job)

    def verified_loop(self):
        """verified 作业超时监视: 连续 verified_timeout_s 零进展 (没有任何
        一件被相机核对确认, 滑动窗口) -> 故障锁存 + 作废需求 (显式失败,
        绝不盲推). 完成路径由 _verified_progress 驱动 (每推一件顺延截止)."""
        while not self._stop.is_set():
            time.sleep(0.5)
            now = time.time()
            with self.verified_lock:
                due = [v for v in self.verified_jobs.values()
                       if now > v["deadline"] and v["remaining"] > 0]
            for v in due:
                job = v["job"]
                mats = sorted({a.get("material") for a in job.get("actions")
                               if a.get("material")})
                job["outcome"] = "fault"
                job["fault"] = {"device": "vision",
                                 "error": (f"verified 超时: 连续 "
                                           f"{self.cfg.get('verified_timeout_s', 90.0):.0f}s "
                                           f"零进展, 仍有 "
                                           f"{v['remaining']} 件 {mats} 未被"
                                           f"相机确认 (没见到匹配物料, 未盲推)"),
                                 "ts": _now()}
                job["e2e_s"] = time.time() - job["t0"]
                job["pushed"] = v["pushed"]
                job["remaining"] = v["remaining"]
                with self.verified_lock:
                    self.verified_jobs.pop(job["job_id"], None)
                self._demand_drop_job(job["job_id"])
                self._latch_fault(job)
                self.stats.job_finished(job)
                self._batch_event(job, "fault")
                self._record_job(job)
                # 故障安全: 尽力停带 (与 _fail 一致, 无应答不阻塞)
                self._conveyor_cmd(job, "pause", required=False)
                self._publish("sort/event/verified",
                               {"ts": _now(), "job_id": job["job_id"],
                                "phase": "timeout",
                                "remaining": v["remaining"]}, retain=True)
            # 完成延时停带 (用户 2026-11-19: 完成任务后 N s 停传送带):
            # 每 tick 检查 (与有无待核对作业无关 — blind 批次完成同样触发):
            # 到期且全闲 (无活动 job / 无排队 / 无待核对) 且带子 running 才停;
            # 新作业开始在 _run_job 里已清除计时, 这里再做一次全闲复核兜底.
            delay = float(self.cfg.get("idle_stop_after_job_s", 0) or 0)
            idle_at = self._idle_stop_at
            if delay > 0 and idle_at is not None and now >= idle_at:
                with self.active_lock:
                    busy = self.active_job is not None
                if not busy:
                    with self.queue_lock:
                        busy = busy or len(self.job_queue) > 0
                if not busy:
                    with self.verified_lock:
                        busy = busy or bool(self.verified_jobs)
                if not busy and self.dev_state.get("conveyor") == "running":
                    self._idle_stop_at = None
                    print(f"[gateway] 任务完成 {delay:.0f}s 无新作业, "
                          f"自动停带", flush=True)
                    self._conveyor_cmd({"job_id": "idle-stop"},
                                       "stop", required=False)

    def cancel_job(self, job_id):
        """撤销作业: verified 待核对 job (作废需求) / 排队中的盲拨 job.
        返回 True=已撤销 / False=执行中无法撤销 / None=作业不存在或已结束."""
        with self.verified_lock:
            vj = self.verified_jobs.get(job_id)
        if vj:
            job = vj["job"]
            job["outcome"] = "cancelled"
            job["e2e_s"] = time.time() - job["t0"]
            job["pushed"] = vj["pushed"]
            job["remaining"] = vj["remaining"]
            with self.verified_lock:
                self.verified_jobs.pop(job_id, None)
            self._demand_drop_job(job_id)
            self._batch_event(job, "cancelled")
            self.stats.job_finished(job)
            self._record_job(job)
            self._publish("sort/event/verified",
                           {"ts": _now(), "job_id": job_id,
                            "phase": "cancelled"}, retain=True)
            print(f"[gateway] verified job {job_id} 已撤销 "
                  f"(pushed={vj['pushed']} remaining={vj['remaining']})",
                  flush=True)
            self._schedule_idle_stop()  # 任务已无待办: 开始停带倒计时
            return True
        removed = None
        with self.queue_lock:
            for j in list(self.job_queue):
                if j["job_id"] == job_id:
                    self.job_queue.remove(j)
                    removed = j
                    break
        if removed is not None:
            removed["outcome"] = "cancelled"
            removed["e2e_s"] = time.time() - removed["t0"]
            self._demand_drop_job(job_id)
            self._batch_event(removed, "cancelled")
            self.stats.job_finished(removed)
            self._record_job(removed)
            self._schedule_idle_stop()  # 排队作业撤销后无待办: 停带倒计时
            return True
        with self.active_lock:
            active = self.active_job
        if active is not None and active["job_id"] == job_id:
            return False
        return None

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
                                      "dispatch_mode": gw.cfg.get("dispatch_mode", "blind"),
                                      "broker_connected": gw._connected.is_set(),
                                      "active_job": (gw.active_job or {}).get("job_id"),
                                      "queue": len(gw.job_queue),
                                      "fault": gw.fault,
                                      # 传送带实时态 (running/stopped/paused/fault): 数字孪生按实际带态显示
                                      "conveyor": gw.dev_state.get("conveyor", "unknown"),
                                      # 带健康监测原因 (固件 BeltWatch: belt ok / belt slip / belt jam)
                                      "conveyor_note": gw.conveyor_note,
                                      # 红外 T0 实时"物品在 IR"态 (0/1): 数字孪生同步反馈
                                      "ir_present": gw.ir_present,
                                      # 红外 T0 上升沿累计 (物体过 IR 次数): 事件驱动, 绝不漏, 供孪生锁存
                                      "ir_edge_count": gw.ir_edge_count,
                                      # 仓距 (2026-11-20 运行时可调): config 值 vs 设备实际值 (遥测)
                                      "bin_dist_mm": gw.cfg.get("bin_dist_mm"),
                                      "bin_dist_device": gw.bin_dist_device,
                                      "bin_dist_in_sync": (
                                          gw.bin_dist_device is not None
                                          and gw.cfg.get("bin_dist_mm") is not None
                                          and [round(float(x), 1) for x in gw.bin_dist_device]
                                          == [round(float(x), 1)
                                              for x in gw.cfg["bin_dist_mm"]]),
                                      # 完成延时停带倒计时余量 (s): 任务完成后
                                      # N s 内无新作业/起带则自动停带; null=无待停
                                      "idle_stop_in_s": (
                                          round(gw._idle_stop_at - time.time(), 1)
                                          if gw._idle_stop_at else None),
                                      # 指令需求台账快照 (2026-11-20):
                                      # 逐 (物料|仓) 剩余件数 — 页面下发列表
                                      # 按此实时减行 (每实推一件减一行)
                                      "demands": gw._demands_snapshot(),
                                      # verified 待核对作业 (2026-11-19 方案 B):
                                      # 逐件核对进度 pushed/remaining + 超时余量
                                      "verified": [
                                          {"job_id": k,
                                           "material": (v["job"].get("actions")
                                                        or [{}])[0].get(
                                                 "material"),
                                           "pushed": v["pushed"],
                                           "remaining": v["remaining"],
                                           "deadline_in_s":
                                               round(v["deadline"]
                                                     - time.time(), 1)}
                                          for k, v in
                                          sorted(gw.verified_jobs.items())]})
                elif self.path == "/stats":
                    s = gw.stats.summary()
                    s["fault"] = gw.fault
                    self._send(200, s)
                elif self.path == "/motion":
                    # 运动事件时间线 (设备状态迁移: 推杆何时推出/光门何时触发):
                    # "让示意图动起来"的动画据此还原实际情况.
                    self._send(200, {"count": len(gw.motion),
                                      "motion": list(gw.motion)})
                elif self.path == "/vision":
                    # 最近视觉识别事件 (HuskyLens 2 实际看到的, 时间序):
                    # 数字孪生按此显示"实际看到的" — 相机识别标签/物品物料.
                    self._send(200, {"count": len(gw.vision_events),
                                      "vision": list(gw.vision_events)[-8:]})
                elif self.path == "/jobs":
                    # 模型 vs 实际对照 (数字孪生核心): 逐批次对照
                    #   命令(laya: 物料/仓/数量) vs 执行(edge101: 成败/逐仓ack)
                    #   vs HuskyLens 实际看到(按时间窗就近关联, 时序近似).
                    # judgment: ok=执行成功且HL物料无意外 / mismatch=HL看到命令外物料
                    #          / fault=执行失败. 选仓权在 PC laya, 网关只对照.
                    with gw.job_history_lock:
                        recs = list(gw.job_history)[-20:]
                    vis = list(gw.vision_events)
                    out = []
                    for r in reversed(recs):
                        actions = r.get("actions") or []
                        cmd_mats = sorted({a.get("material")
                                           for a in actions if a.get("material")})
                        dres = r.get("divert_results") or []
                        exec_ok = (r.get("outcome") == "done"
                                   and bool(dres)
                                   and all(d.get("ok") for d in dres))
                        t0 = float(r.get("t0") or 0.0)
                        t1 = float(r.get("t_done") or 0.0) + 2.0
                        hl = [v for v in vis
                              if (v.get("t") or 0.0) >= t0 - 2.0
                              and (v.get("t") or 0.0) <= t1]
                        hl_mats = sorted({v.get("material") for v in hl})
                        mismatch = sorted(set(hl_mats) - set(cmd_mats))
                        if r.get("outcome") == "cancelled":
                            judgment = "cancelled"
                        elif r.get("outcome") != "done":
                            judgment = "fault"
                        elif mismatch:
                            judgment = "mismatch"
                        else:
                            judgment = "ok"
                        out.append({
                            "job_id": r.get("job_id"),
                            "ts": r.get("ts"),
                            "source": r.get("source"),
                            "outcome": r.get("outcome"),
                            "fault": r.get("fault"),
                            "actions": actions,
                            "pushed": r.get("pushed"),
                            "remaining": r.get("remaining"),
                            "divert_results": dres,
                            "cmd_materials": cmd_mats,
                            "exec_ok": exec_ok,
                            "hl_materials": hl_mats,
                            "hl_conf": [round(v.get("confidence", 0.0), 2)
                                        for v in hl],
                            "hl_mismatch": mismatch,
                            "judgment": judgment,
                            "e2e_ms": r.get("e2e_ms"),
                        })
                    self._send(200, {"count": len(out), "jobs": out})
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
                    resp = {"job_id": job["job_id"],
                            "queued": len(gw.job_queue)}
                    if gw.cfg.get("dispatch_mode") == "verified":
                        resp["verified"] = True  # 已登记需求, 等相机逐件核对
                    self._send(202, resp)
                elif self.path == "/config/apply":
                    # 仓距下发 (2026-11-20): 推给固件 (校验+落 NVS), 免编译/烧录.
                    # 值来源: 请求体 {"bin_dist_mm":[d1,d2,d3]} 优先, 否则用
                    # 启动时加载的 config (改 config/sort_gateway.json 需重启
                    # 网关或直接在请求体传值).
                    if not gw._connected.is_set():
                        self._send(503, {"error": "gateway mqtt 未连接"})
                        return
                    vals = body.get("bin_dist_mm") or gw.cfg.get("bin_dist_mm")
                    if vals is None:
                        self._send(500, {"ok": False,
                                         "note": "无 bin_dist_mm (config 未配且请求体未传)"})
                        return
                    result = gw._push_bin_dist_with(vals, "http /config/apply",
                                                    wait_s=float(gw.cfg["ack_timeout_s"]))
                    if result == "ok":
                        self._send(200, {"ok": True, "bin_dist_mm": vals,
                                         "note": "设备已应用并落 NVS (掉电保持)"})
                    elif result is None:
                        self._send(502, {"ok": False, "bin_dist_mm": vals,
                                         "note": "设备未应答 (未上线? 超时)"})
                    else:
                        rstr = str(result)
                        note = rstr.split(": ", 1)[1] if rstr.startswith("fault: ") \
                            else rstr
                        self._send(400, {"ok": False, "bin_dist_mm": vals,
                                         "note": "设备校验拒绝: " + note})
                elif self.path == "/fault/clear":
                    had = gw.clear_fault("http")
                    self._send(200, {"cleared": had is not None, "had": had})
                elif self.path == "/dispatch/cancel":
                    # 撤销作业 (2026-11-19 方案 B): verified 待核对 job 作废
                    # 需求; 排队中的盲拨 job 移除. 执行中 (已发固件) 409.
                    job_id = body.get("job_id")
                    r = gw.cancel_job(job_id) if job_id else None
                    if r is True:
                        self._send(200, {"cancelled": job_id})
                    elif r is False:
                        self._send(409,
                                   {"error": "作业执行中 (固件在推), 不能撤销"})
                    else:
                        self._send(404,
                                   {"error": "作业不存在或已结束"})
                elif self.path == "/belt":
                    # 原子传送带控制 (网页按钮): start/stop/resume/pause -> 固件 conveyor/cmd
                    action = body.get("action")
                    if action not in ("start", "stop", "resume", "pause"):
                        self._send(422, {"error": "action 需 start|stop|resume|pause"})
                        return
                    if gw.fault:
                        self._send(409, {"error": "fault latched, clear first",
                                        "fault": gw.fault})
                        return
                    job = {"job_id": gw._new_job_id()}
                    ok, note = gw._conveyor_cmd(job, action)
                    if ok and action in ("start", "resume"):
                        gw._idle_stop_at = None  # 手动起带: 撤销待停
                    self._send(200 if ok else 502,
                               {"ok": ok, "note": note, "action": action})
                elif self.path == "/push":
                    # 原子手动推杆 (网页按钮): push <n> -> 固件 sorter{n}/cmd action=push
                    # 立即 ack (jog 展开->收回由固件驱动), motion busy/ready 供孪生同步
                    try:
                        n = int(body.get("sorter", 0))
                    except Exception:
                        n = 0
                    if n not in (1, 2, 3):
                        self._send(422, {"error": "sorter 需 1|2|3"})
                        return
                    if gw.fault:
                        self._send(409, {"error": "fault latched, clear first",
                                        "fault": gw.fault})
                        return
                    job_id = gw._new_job_id()
                    payload = {"job_id": job_id, "action": "push", "ts": _now()}
                    # 手动推杆: 一次性等待, busy 立即返回 (jog 只在收到命令时 ack 一次,
                    # 不重 ack; 若用 _send_with_retry 的 busy 延长逻辑会白等 3x3s).
                    if not gw.client:
                        self._send(503, {"error": "gateway mqtt not connected"})
                        return
                    gw.client.publish(gw._cmd_topic("sorter", "sorter%d" % n),
                                      json.dumps(payload), qos=1)
                    result, note = gw._wait_ack("sorter", "sorter%d" % n,
                                                job_id, float(gw.cfg["ack_timeout_s"]))
                    if result == "ok":
                        self._send(200, {"ok": True, "job_id": job_id,
                                         "note": "jog", "sorter": n})
                    elif result == "busy":
                        self._send(409, {"ok": False, "sorter": n,
                                         "note": note or "推杆忙: 另一推杆正在动作, 稍后再试"})
                    else:
                        self._send(502, {"ok": False, "sorter": n,
                                         "note": note or "推杆未应答 (设备未上线?)"})
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
        # 仓距下发 (2026-11-20): 启动即推 (fire-and-forget: 设备可能还没
        # 上线; 设备上线后遥测断口检测会重推, 不丢).
        self._push_bin_dist("gateway startup", wait_s=None)
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.stats_loop, daemon=True).start()
        threading.Thread(target=self.verified_loop, daemon=True).start()
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
