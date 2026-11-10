"""MQTT 设备模拟器: 1 传送带 + 3 分拣执行器 (无硬件时闭环测试 + 设备侧行为参照).

镜像 edge101 执行器固件 (2026-11-10 新方案, commit 8fceaf2): 网关 = 指令源
(物料,仓,数量), 执行器只做物理动作, 不选仓. 逐件执行:
  单点红外 (IR, T0) -> 带速外推预测到目标仓推杆 -> 到点瞬停 -> 推杆展开
  (hold=0) -> 收回 -> 恢复带. HuskyLens 视觉 = 上报 (不选仓), 模拟按指令运行,
  忽略视觉 (执行只跟 (物料,仓,数量), 不受视觉影响).

每个设备 = 状态机 + 周期遥测 (2Hz):
  传送带: stopped --start--> running --pause--> paused --stop--> stopped
          带健康故障注入 (slip/jam) -> fault (reset 恢复)
  分拣器: ready --divert--> armed --(每件: IR 到仓 -> 停带推杆)--> ready
          (全推完才回 ok); 无 IR 沿/无进展超时 -> 瞬态 fault (同固件, 可再接指令);
          --no-ack 时收到 divert 不回应 (测网关超时)

真实 PLC/控制器实现时, 只需满足 docs/sort-mqtt.md 的 命令/应答/遥测 三组 topic,
本网关即可直接接管.

故障注入: --fault-rate 0.1 (随机故障) --no-ack (divert 永不响应)
          --latency-ms 200 (应答延迟) --fault-sorter 2 (指定分拣器固定故障)
          --item-ms 900 (每件推杆行程窗口: 停带->展开->hold->收回->恢复, 默认 900)
          --ir-wait 0.15 (物品过 IR -> 到仓 行程 s) --ir-timeout 15.0 (无 IR 超时 s)
          --ir-dead-sorter 2 (指定分拣器 IR 永远见不到物品, 测超时 fault)

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
        # 带健康故障注入 (镜像固件 BeltWatch 的 SLIP/JAM 上报通道):
        # belt_fault="slip"|"jam" 时, 带子 running 后报 conveyor 遥测 fault +
        # motion fault 事件 (note 同固件 reason), 验证网关锁存/恢复链路.
        self.belt_fault = getattr(gw_args, "belt_fault", None) or ""
        self.belt_fault_fired = False

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
            if (self.belt_fault and not self.belt_fault_fired
                    and self.items_in >= 2):
                self._fire_belt_fault()
        d = {"state": self.state, "speed_mps": self.speed,
             "items_in": self.items_in}
        if self.state == "fault" and self.belt_fault_fired:
            d["note"] = ("belt slip: measured 0.72 vs commanded 1.00 m/s"
                         if self.belt_fault == "slip"
                         else "belt jam: item stalled between gates (simulated)")
        return d

    def _fire_belt_fault(self):
        """带健康故障 (镜像固件 BeltWatch): 停带 + 锁存 + motion 事件."""
        self.state = "fault"
        self.speed = 0.0
        self.belt_fault_fired = True
        via = "belt-slip" if self.belt_fault == "slip" else "belt-jam"
        self.client.publish("sort/line1/motion", json.dumps(
            {"ts": _now(), "sorter": 1, "state": "fault", "via": via,
             "job_id": "", "item": 0, "of": 0, "src": "sim"}), qos=0)
        print(f"[sim:conveyor] 带健康故障注入: {via} (遥测 fault + motion)",
              flush=True)


class SorterSim(SimDevice):
    """分拣执行器 (镜像 edge101 执行器固件, 2026-11-10 新方案):
    单点红外 (IR, T0) -> 带速外推预测到目标仓推杆 -> 到点瞬停 -> 推杆展开
    (hold=0) -> 收回 -> 恢复带. 逐件执行, 全推完才 ack ok.

    网关 = 指令源 (物料,仓,数量); 本执行器只做物理动作, 不选仓.
    HuskyLens 视觉 = 上报 (不选仓); 模拟按指令运行, 忽略视觉 (执行只跟指令).
    命令/应答/遥测/运动事件 契约与固件 publishMotion/publishSorterAck 一致:
      armed(via=cmd)     -> 接受指令, 等物品过 IR
      busy(via=ir_due)   -> 物品到仓, 停带 + 推杆展开
      armed(via=next)    -> 已推 k 件, 等下一件过 IR (k<qty)
      ready(via=done)    -> 全推完
      fault(via=timeout) -> 无 IR 沿/无进展超时 (IR 未见物品)
      ready(via=cancel)  -> reset 取消
    故障为瞬态 (同固件 curCmd.active=false: 报完 fault 后可再接新指令),
    锁存由网关做. 带健康 (slip/jam) 在传送带侧, 不在这里.
    """

    def __init__(self, gw_args, rng, n, fixed_fault=False):
        self.n = n
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
        # 推杆行程窗口 (ms): 停带 -> 展开(-80°) -> hold(0) -> 收回(270°) -> 恢复.
        # 镜像固件 SERVO_EJECT_MOVE_MS + hold + SERVO_RETRACT_MOVE_MS.
        self.item_ms = getattr(gw_args, "item_ms", 900)
        # 红外 (IR, T0): 物品过 IR -> 带速外推 -> 到目标仓. 模拟固定行程/超时.
        self.ir_wait_s = getattr(gw_args, "ir_wait", 0.15)        # IR -> 到仓 行程 (s)
        self.ir_timeout_s = getattr(gw_args, "ir_timeout", 15.0)  # 无 IR 沿/无进展 超时 (s)
        self.ir_dead = getattr(gw_args, "ir_dead", False)         # IR 永远见不到物品
        self.cur_job = None                       # 当前 job_id (运动事件关联)

    def _motion(self, via, state=None, item=0, of=0):
        """运动事件: 状态迁移时间线 (与固件 sort/line1/motion 契约一致).

        via: cmd/ir_due/next/done/timeout/cancel/reset/injected
        (injected = 模拟随机故障注入, 固件侧无对应). 网关维护 GET /motion
        时间线, 供"让示意图动起来"的动画还原实际情况.
        item/of: 逐件件号 (qty>1 时每件各一条事件序列, 与固件逐件推件一致).
        """
        payload = {"sorter": self.n, "state": state or self.state, "via": via,
                   "job_id": self.cur_job or "", "ts": _now(), "src": "sim"}
        if of > 0 and item > 0:
            payload["item"] = item
            payload["of"] = of
        self.client.publish("sort/line1/motion", json.dumps(payload), qos=0)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        client.subscribe(self.topic, qos=1)
        print(f"[sim:{self.topic}] 已连接 (state={self.state})", flush=True)

    def handle_cmd(self, payload):
        action = payload.get("action")
        job_id = payload.get("job_id", "?")
        if action == "reset":
            if self.state == "fault":
                self.state = "ready"
                self._ack(job_id, action, "ok")
                self._motion("reset", "ready")
            elif self.state in ("armed", "busy"):
                self.state = "ready"      # 取消执行中指令: 不推料
                self._ack(job_id, action, "ok", note="cancelled")
                self._motion("cancel", "ready")
            else:
                self._ack(job_id, action, "ok", note="idle")   # 幂等目标态: no-op ok
            return
        if action != "divert":
            return
        if self.state == "fault":
            self._ack(job_id, action, "fault", note="sorter fault")
            return
        if self.state in ("armed", "busy"):
            # 与固件 Sorter::handleCmd 一致: 执行中只回 busy-ack, 不重复执行
            self._ack(job_id, action, "busy", note=f"state={self.state}")
            return
        qty = int(payload.get("qty", 1))
        self.last_material = payload.get("material")
        self.cur_job = job_id
        # 接受指令: 进入 armed, 等物品过 IR (同固件 publishMotion armed/cmd).
        # 不立即 ack: 全部推完才 ack ok (同 .bak 语义, 网关据此等待).
        self.state = "armed"
        self._motion("cmd", "armed", item=1, of=qty)

        def run_all():
            for k in range(1, qty + 1):
                if self.ir_dead:
                    # 期望的物品没到 IR (无沿/无进展) -> 超时 fault (同固件
                    # IR_WAIT_TIMEOUT_MS). 故障瞬态: 报完回 ready, 可再接指令.
                    time.sleep(self.ir_timeout_s)
                    if self.state in ("armed", "busy"):
                        self._ack(job_id, action, "fault",
                                  note="IR timeout / no item at T0")
                        self._motion("timeout", "fault", item=k, of=qty)
                        self.state = "ready"
                    return
                time.sleep(self.ir_wait_s)          # 物品过 IR -> 带速外推 -> 到仓
                if self.state not in ("armed",):   # 等待期间被 reset 取消
                    return
                self.state = "busy"                # 到点瞬停 + 推杆展开
                self._motion("ir_due", "busy", item=k, of=qty)
                time.sleep(self.item_ms / 1000.0)  # 停带 -> 展开 -> hold -> 收回 -> 恢复带
                self.diverted_total += 1
                if k < qty:
                    self.state = "armed"           # 等下一件过 IR
                    self._motion("next", "armed", item=k, of=qty)
                else:
                    self.state = "ready"
                    if self.rng.random() < self.fault_rate:
                        self._ack(job_id, action, "fault", note="injected")
                        self._motion("injected", "fault", item=qty, of=qty)
                    else:
                        self._ack(job_id, action, "ok", note=self.last_material or "")
                        self._motion("done", "ready", item=qty, of=qty)

        threading.Thread(target=run_all, daemon=True).start()

    def tele_extra(self):
        # 与固件一致: 有执行中指令 (armed/busy) 遥测报 busy, 否则 ready; 故障报 fault.
        st = ("fault" if self.state == "fault"
              else "busy" if self.state in ("armed", "busy") else "ready")
        return {"state": st, "diverted_total": self.diverted_total,
                "last_material": self.last_material}


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="tcp://127.0.0.1:1883")
    ap.add_argument("--latency-ms", type=int, default=30)
    ap.add_argument("--fault-rate", type=float, default=0.0)
    ap.add_argument("--fault-sorter", type=int, default=0,
                    help="指定 1/2/3 号分拣器固定故障")
    ap.add_argument("--item-ms", type=int, default=900,
                    help="分拣器每件推杆行程窗口 ms (停带->展开->hold->收回->恢复, "
                         "镜像固件 SERVO_EJECT+hold+SERVO_RETRACT)")
    ap.add_argument("--ir-wait", type=float, default=0.15,
                    help="物品过 IR(T0) -> 带速外推 -> 到仓 行程 s (模拟固定延迟)")
    ap.add_argument("--ir-timeout", type=float, default=15.0,
                    help="无 IR 沿/无进展 超时 s (镜像固件 IR_WAIT_TIMEOUT_MS)")
    ap.add_argument("--ir-dead-sorter", type=int, default=0,
                    help="指定 1/2/3 号分拣器 IR 永远见不到物品 (测超时 fault)")
    ap.add_argument("--no-ack", action="store_true",
                    help="分拣器 divert 永不回应 (测网关超时/重试)")
    ap.add_argument("--belt-fault", choices=["slip", "jam"], default=None,
                    help="带健康故障注入 (镜像固件 BeltWatch): 带子 running 后 "
                         "报 conveyor 遥测 fault + motion 事件 (slip=打滑, jam=卡滞)")
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
    devs: list = [ConveyorSim(a, rng)]
    for n in (1, 2, 3):
        devs.append(SorterSim(a, rng, n, fixed_fault=(a.fault_sorter == n)))
        devs[-1].ir_dead = (a.ir_dead_sorter == n)
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
