"""MQTT 设备模拟器: 1 传送带 + 3 分拣器 (无硬件时闭环测试 + 设备侧行为参照).

每个设备 = 状态机 + 周期遥测 (2Hz):
  传送带: stopped --start--> running --pause--> paused --stop--> stopped
          故障注入 -> fault (reset 恢复)
  分拣器: ready --divert--> busy --(item_ms × qty, 全拨完才回 ok)--> ready
          故障 -> fault (reset 恢复); --no-ack 时收到 divert 不回应 (测网关超时)

真实 PLC/控制器实现时, 只需满足 docs/sort-mqtt.md 的 命令/应答/遥测 三组 topic,
本网关即可直接接管.

故障注入: --fault-rate 0.1 (随机故障) --no-ack (divert 永不响应)
          --latency-ms 200 (应答延迟) --fault-sorter 2 (指定分拣器固定故障)
          --item-ms 900 (每件拨料耗时, 镜像固件 0.9s/件; 默认 900)

Run: .venv/bin/python sim_devices.py [--broker tcp://127.0.0.1:1883] [--seed 7]
"""

import argparse
import json
import re
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SimDevice:
    """设备基类: 订阅自己的 cmd topic, 周期发遥测, ack 带可配延迟."""

    topic = ""           # 子类覆盖
    act_ms = 150.0      # 动作完成耗时

    def __init__(self, gw_args, rng):
        self.broker_host, self.broker_port = gw_args.broker_host, gw_args.broker_port
        self.latency = gw_args.latency_ms / 1000.0
        self.no_ack = gw_args.no_ack
        self.fault_rate = gw_args.fault_rate
        self.rng = rng
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id="sim-" + self.topic + "-"
                                             + str(abs(hash(self.topic)) % 1000))
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_msg
        self._stop = threading.Event()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        client.subscribe(self.topic, qos=1)
        print(f"[sim:{self.topic}] 已连接, 订阅 {self.topic}", flush=True)

    def _on_msg(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode() or "{}")
        except Exception:
            return
        self.handle_cmd(payload)

    def handle_cmd(self, payload):
        raise NotImplementedError

    def _ack(self, job_id, action, result, note=""):
        if self.no_ack and action == "divert":
            return
        time.sleep(self.latency)
        topic = self.topic.replace("/cmd", "/ack") + "/" + str(job_id)
        self.client.publish(topic, json.dumps(
            {"job_id": job_id, "action": action, "result": result,
             "note": note, "ts": _now()}), qos=1)

    def _tele(self, extra):
        payload = dict(extra)
        payload["ts"] = _now()
        self.client.publish(self.topic.replace("/cmd", "/telemetry"),
                              json.dumps(payload), qos=0)

    def tele_loop(self, extra_fn):
        while not self._stop.is_set():
            self._tele(extra_fn())
            time.sleep(0.5)

    def start(self):
        self.client.connect(self.broker_host, self.broker_port)
        self.client.loop_start()
        threading.Thread(target=self.tele_loop,
                           kwargs={"extra_fn": self.tele_extra}, daemon=True).start()

    def tele_extra(self):
        return {}

    def stop(self):
        self._stop.set()
        self.client.loop_stop()
        self.client.disconnect()


class ConveyorSim(SimDevice):
    """传送带: start/stop/pause/resume/reset, running 时 items_in 递增."""

    topic = "sort/line1/conveyor/cmd"
    act_ms = 80.0

    def __init__(self, gw_args, rng):
        super().__init__(gw_args, rng)
        self.state = "stopped"
        self.items_in = 0
        self.speed = 0.0

    def handle_cmd(self, payload):
        action = payload.get("action")
        job_id = payload.get("job_id", "?")
        # 幂等目标态语义 (与固件 Conveyor 一致): "已处于目标态" 回 ok.
        # 网关把 busy-ack 解释为 "设备正在执行本 job" 并延长等待,
        # stop 已停若回 busy 会让网关空等.
        if action in ("start", "resume") and self.state != "fault":
            if action == "resume" and self.state == "stopped":
                self._ack(job_id, action, "busy", note="state=stopped")
                return
            if self.state != "running":
                self.state = "running"
            self.speed = float(payload.get("speed_mps", self.speed or 1.0))
            self._ack(job_id, action, "ok", note=f"state={self.state}")
            return
        if action == "stop" and self.state != "fault":
            if self.state != "stopped":
                self.state = "stopped"
                self.speed = 0.0
            self._ack(job_id, action, "ok", note=f"state={self.state}")
            return
        if action == "pause":
            if self.state == "running":
                self.state = "paused"
                self._ack(job_id, action, "ok", note=f"state={self.state}")
                return
            if self.state == "paused":
                self._ack(job_id, action, "ok", note="state=paused")
                return
        if action == "reset":
            if self.state == "fault":
                self.state = "stopped"
                self.speed = 0.0
            self._ack(job_id, action, "ok", note=f"state={self.state}")
            return
        if action in ("start", "stop", "pause", "resume", "reset"):
            self._ack(job_id, action, "busy", note=f"state={self.state}")
        # 故障注入
        if self.fault_rate and self.rng.random() < self.fault_rate \
                and self.state == "running":
            self.state = "fault"
            self.speed = 0.0

    def tele_extra(self):
        if self.state == "running":
            self.items_in += 1
        return {"state": self.state, "speed_mps": self.speed,
                "items_in": self.items_in}


class SorterSim(SimDevice):
    """分拣器: divert -> busy -> ready (diverted_total += qty)."""

    def __init__(self, gw_args, rng, n, fixed_fault=False):
        self.broker_host, self.broker_port = gw_args.broker_host, gw_args.broker_port
        self.latency = gw_args.latency_ms / 1000.0
        self.no_ack = gw_args.no_ack
        self.fault_rate = gw_args.fault_rate
        self.rng = rng
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"sim-sorter{n}-{abs(hash(n)) % 1000}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_msg
        self._stop = threading.Event()
        self.topic = f"sort/sorter{n}/cmd"
        self.state = "fault" if fixed_fault else "ready"
        self.diverted_total = 0
        self.last_material = None
        self.item_ms = getattr(gw_args, "item_ms", 900)   # 每件拨料耗时 (ms)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        client.subscribe(self.topic, qos=1)
        print(f"[sim:{self.topic}] 已连接 (state={self.state})", flush=True)

    def handle_cmd(self, payload):
        action = payload.get("action")
        job_id = payload.get("job_id", "?")
        if action == "reset" and self.state == "fault":
            self.state = "ready"
            self._ack(job_id, action, "ok")
            return
        if action != "divert":
            return
        if self.state == "fault":
            self._ack(job_id, action, "fault", note="sorter fault")
            return
        if self.state == "busy":
            # 与固件 Sorter::handleCmd 一致: busy 时只回 busy-ack, 不重复执行
            self._ack(job_id, action, "busy", note="state=busy")
            return
        qty = int(payload.get("qty", 1))
        self.last_material = payload.get("material")
        self.state = "busy"

        def finish():
            time.sleep(self.item_ms * qty / 1000.0)   # qty 件全拨完才回 ok
            if self.rng.random() < self.fault_rate:
                self.state = "fault"
                self._ack(job_id, action, "fault", note="injected")
            else:
                self.diverted_total += qty
                self.state = "ready"
                self._ack(job_id, action, "ok", note=self.last_material)

        threading.Thread(target=finish, daemon=True).start()

    def tele_extra(self):
        return {"state": self.state, "diverted_total": self.diverted_total,
                "last_material": self.last_material}


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="tcp://127.0.0.1:1883")
    ap.add_argument("--latency-ms", type=int, default=30)
    ap.add_argument("--fault-rate", type=float, default=0.0)
    ap.add_argument("--fault-sorter", type=int, default=0,
                    help="指定 1/2/3 号分拣器固定故障")
    ap.add_argument("--item-ms", type=int, default=900,
                    help="分拣器每件拨料耗时 ms (镜像固件: 拨出 400ms + 件间 500ms)")
    ap.add_argument("--no-ack", action="store_true",
                    help="分拣器 divert 永不回应 (测网关超时/重试)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    m = re.match(r"tcp://([^:/]+):(\d+)$", a.broker)
    if m:
        a.broker_host, a.broker_port = m.group(1), int(m.group(2))
    else:
        h, _, p = a.broker.partition(":")
        a.broker_host, a.broker_port = h or "127.0.0.1", int(p or 1883)
    return a


def main():
    import random
    a = parse_args()
    rng = random.Random(a.seed)
    devs = [ConveyorSim(a, rng)]
    for n in (1, 2, 3):
        devs.append(SorterSim(a, rng, n, fixed_fault=(a.fault_sorter == n)))
    for d in devs:
        d.start()
    print(f"[sim] 就绪: 传送带 + 3 分拣器 "
          f"(latency={a.latency_ms}ms fault_rate={a.fault_rate} "
          f"no_ack={a.no_ack} fault_sorter={a.fault_sorter or '-'})",
          flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for d in devs:
            d.stop()


if __name__ == "__main__":
    main()
