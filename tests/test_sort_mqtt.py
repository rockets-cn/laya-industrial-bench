"""MQTT 分拣网关 E2E (进程内 amqtt broker + 模拟设备 + 网关, 无外部依赖).

Run: .venv/bin/python tests/test_sort_mqtt.py

覆盖:
 1. 批次下发 -> 网关 -> MQTT -> 模拟设备 -> 应答 -> 统计/台账
 2. 命令无应答 -> 重试 -> 故障锁存 -> 人工清除 -> 恢复
 3. 视觉入站 sort/inbound/vision -> 物料映射 -> divert (含未知/低置信)
 4. SCADA 入站命令 topic sort/inbound/command
 5. 非法 actions 拒绝 (白名单)
 6. sort/stats/summary 周期/retain 统计发布
 7. 视觉传感器 E2E: 合成帧 -> HSVBlockVision -> vision 事件 -> divert
"""

import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import paho.mqtt.client as mqtt  # noqa: E402
import amqtt.broker as ab  # noqa: E402

import sort_mqtt as sm  # noqa: E402
import sim_devices as sd  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  FAIL {name}  {detail}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_broker(port):
    loop = asyncio.new_event_loop()

    async def boot():
        b = ab.Broker({"listeners": {"default": {
            "type": "tcp", "bind": f"127.0.0.1:{port}"}}})
        await b.start()
        await asyncio.Event().wait()

    t = threading.Thread(target=lambda: (asyncio.set_event_loop(loop),
                                         loop.run_until_complete(boot())),
                          daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("broker 未起来")


def wait_for(pred, timeout=8.0, what=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    print(f"  [超时] {what}")
    return False


def http_json(url, body=None, timeout=5):
    if body is not None:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def make_args(no_ack=False):
    class A:  # 模拟 CLI 参数
        pass
    a = A()
    a.broker_host, a.broker_port = "127.0.0.1", BPORT
    a.latency_ms, a.fault_rate, a.no_ack = 20, 0.0, no_ack
    return a


def main():
    global BPORT, GPORT
    BPORT, GPORT = free_port(), free_port()
    start_broker(BPORT)
    ledger = os.path.join(HERE, "stats", "test_ledger.jsonl")
    if os.path.exists(ledger):
        os.remove(ledger)
    cfg = {"broker": f"tcp://127.0.0.1:{BPORT}", "http_port": GPORT,
           "ack_timeout_s": 0.8, "ack_retries": 1, "stats_period_s": 1.0,
           "ledger_path": ledger}
    gw = sm.SortGateway(cfg).start()
    print(f"[test] broker :{BPORT}  gateway HTTP :{GPORT}  ledger={ledger}")

    import random
    rng = random.Random(7)
    conv = sd.ConveyorSim(make_args(), rng)
    s1, s2, s3 = (sd.SorterSim(make_args(), rng, n) for n in (1, 2, 3))
    for d in (conv, s1, s2, s3):
        d.start()
    time.sleep(1.0)

    # ---------- 1. 批次下发 E2E ----------
    print("\n== 1. 批次下发 E2E ==")
    base_stats = gw.stats.batches["done"]
    st, body = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                         {"source": "test",
                          "actions": [{"yard": 1, "material": "red_apple", "qty": 2},
                                      {"yard": 2, "material": "banana", "qty": 3},
                                      {"yard": 3, "material": "blue_block", "qty": 5}]})
    check("dispatch 202", st == 202, f"st={st} body={body}")
    job_id = body.get("job_id")
    ok = wait_for(lambda: gw.stats.batches["done"] > base_stats, 10, "批次 done")
    check("批次完成", ok)
    time.sleep(0.3)
    check("分拣器计数 2/3/5", (s1.diverted_total, s2.diverted_total, s3.diverted_total) == (2, 3, 5),
          f"got {(s1.diverted_total, s2.diverted_total, s3.diverted_total)}")
    s = gw.stats.summary()
    check("统计 items=10", s["items"]["dispatched"] == 10, f"got {s['items']}")
    check("统计 by_sorter", s["items"]["by_sorter"] == {"1": 2, "2": 3, "3": 5},
          f"got {s['items']['by_sorter']}")
    check("统计 by_material", s["items"]["by_material"].get("red_apple") == 2
          and s["items"]["by_material"].get("banana") == 3
          and s["items"]["by_material"].get("blue_block") == 5)
    lines = [json.loads(l) for l in open(ledger)]
    check("台账 1 行 done", len(lines) == 1 and lines[0]["outcome"] == "done",
          f"lines={lines}")
    check("台账 job_id 一致", lines and lines[0]["job_id"] == job_id)

    # ---------- 2. 无应答 -> 重试 -> 故障锁存 -> 清除 -> 恢复 ----------
    print("\n== 2. 故障锁存与恢复 ==")
    s2.stop()
    s2b = sd.SorterSim(make_args(no_ack=True), rng, 2)
    s2b.start()
    time.sleep(0.5)
    base_fault = gw.stats.batches["fault"]
    st, _ = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                      {"source": "test",
                       "actions": [{"yard": 2, "material": "green_block", "qty": 1}]})
    check("故障期 dispatch 202 入队", st == 202, f"st={st}")
    ok = wait_for(lambda: gw.stats.batches["fault"] > base_fault,
                  10, "故障锁存 (2 次超时)")
    check("ack 超时 -> 故障锁存", ok)
    check("fault 已置位", gw.fault is not None and "sorter2" in (gw.fault.get("device") or ""),
          f"fault={gw.fault}")
    st, body = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                         {"actions": [{"yard": 1, "material": "pear", "qty": 1}]})
    check("锁存期 dispatch 409", st == 409, f"st={st} body={body}")
    st, body = http_json(f"http://127.0.0.1:{GPORT}/fault/clear", {})
    check("人工清除 200", st == 200 and body.get("cleared"), f"st={st} body={body}")
    check("fault 已清除", gw.fault is None)
    # 换回正常应答的 2 号分拣器 (no_ack 设备只在故障窗口内使用)
    s2b.stop()
    s2c = sd.SorterSim(make_args(), rng, 2)
    s2c.start()
    time.sleep(0.8)
    base_done = gw.stats.batches["done"]
    st, _ = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                      {"actions": [{"yard": 1, "material": "pear", "qty": 1}]})
    check("清除后 dispatch 202", st == 202, f"st={st}")
    ok = wait_for(lambda: gw.stats.batches["done"] > base_done, 10, "恢复后 done")
    check("恢复后批次完成", ok)

    # ---------- 3. 视觉入站 (MQTT 直接发事件) ----------
    print("\n== 3. 视觉入站 sort/inbound/vision ==")
    pc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-pub")
    pc.connect("127.0.0.1", BPORT)
    pc.loop_start()
    v_base = dict(gw.stats.vision)

    def pub(topic, payload):
        pc.publish(topic, json.dumps(payload), qos=1)

    pub("sort/inbound/vision", {"material": "red_block", "confidence": 0.9,
                                 "frame_id": "t-1", "source": "test"})
    ok = wait_for(lambda: gw.stats.vision["dispatched"] == v_base["dispatched"] + 1,
                  8, "vision 正常物料 dispatched")
    check("vision 正常物料 dispatched", ok, f"got {gw.stats.vision}")
    pub("sort/inbound/vision", {"material": "durian", "confidence": 0.99,
                                 "frame_id": "t-2", "source": "test"})
    ok = wait_for(lambda: gw.stats.vision["unknown"] == v_base["unknown"] + 1,
                  8, "vision 未知物料")
    check("vision 未知物料 unknown", ok, f"got {gw.stats.vision}")
    pub("sort/inbound/vision", {"material": "blue_block", "confidence": 0.1,
                                 "frame_id": "t-3", "source": "test"})
    ok = wait_for(lambda: gw.stats.vision["low_conf"] == v_base["low_conf"] + 1,
                  8, "vision 低置信")
    check("vision 低置信 low_conf", ok, f"got {gw.stats.vision}")

    # ---------- 4. SCADA 入站命令 ----------
    print("\n== 4. SCADA 入站 sort/inbound/command ==")
    base_total = gw.stats.batches["total"]
    pub("sort/inbound/command",
        {"source": "scada-test", "path": "MQTT",
         "actions": [{"yard": 3, "material": "yellow_block", "qty": 2}]})
    deadline = time.time() + 8
    while time.time() < deadline:
        lines = [json.loads(l) for l in open(ledger)]
        if any(l.get("source") == "scada-test" for l in lines):
            break
        time.sleep(0.1)
    lines = [json.loads(l) for l in open(ledger)]
    sc = [l for l in lines if l.get("source") == "scada-test"]
    check("SCADA 批次入账", len(sc) == 1 and sc[0]["outcome"] == "done", f"sc={sc}")
    check("SCADA 分拣器 3 计数+2", s3.diverted_total == 7, f"got {s3.diverted_total}")

    # ---------- 5. 非法 actions 拒绝 ----------
    print("\n== 5. 白名单拒绝 ==")
    base_drop = gw.stats.batches["queue_dropped"]
    bad = [{"actions": [{"yard": 9, "material": "pear", "qty": 1}]},
           {"actions": [{"yard": 1, "material": "durian", "qty": 1}]},
           {"actions": [{"yard": 1, "material": "pear", "qty": 0}]},
           {"actions": [{"yard": 1, "material": "pear", "qty": 100}]}]
    sts = []
    for b in bad:
        st, _ = http_json(f"http://127.0.0.1:{GPORT}/dispatch", b)
        sts.append(st)
    check("非法 actions 全 422", all(st == 422 for st in sts), f"sts={sts}")
    check("queue_dropped +4", gw.stats.batches["queue_dropped"] == base_drop + 4,
          f"got {gw.stats.batches['queue_dropped']}")

    # ---------- 6. 4. sort/stats/summary (retain) ----------
    print("\n== 6. 统计 summary (retain) ==")
    summary = []

    def on_sum(client, userdata, msg):
        try:
            summary.append(json.loads(msg.payload.decode()))
        except Exception:
            pass

    rc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-sum")
    rc.on_message = on_sum
    rc.connect("127.0.0.1", BPORT)
    rc.subscribe("sort/stats/summary", qos=1)
    rc.loop_start()
    ok = wait_for(lambda: summary, 5, "summary retain 送达")
    check("summary 订阅即达 (retain)", ok)
    ok = wait_for(lambda: summary and summary[-1]["batches"]["total"] >= 5,
                  8, "summary 周期刷新计数追平")
    check("summary 周期刷新 (最新 retain 值)", ok,
          f"latest={summary[-1]['batches'] if summary else None}")
    if summary:
        s6 = summary[-1]
        keys = ("ts", "batches", "items", "cmd", "vision",
                "latency_ms", "throughput_items_per_min")
        check("summary 字段齐全", all(k in s6 for k in keys), f"keys={list(s6)}")
        check("summary batches>=5", s6["batches"]["total"] >= 5,
              f"got {s6['batches']}")
        check("summary items>=13", s6["items"]["dispatched"] >= 13,
              f"got {s6['items']['dispatched']}")
    rc.loop_stop(); rc.disconnect()

    # ---------- 7. 视觉传感器 E2E (合成帧 -> HSV 识别 -> divert) ----------
    print("\n== 7. 视觉传感器 E2E (合成帧) ==")
    import vision_input as vi  # noqa: E402
    import bench_colorblock as bc  # noqa: E402
    vision = vi.HSVBlockVision(frac_min=0.08)
    frame = bc.make_test_frames()[0][1]  # 单色-红
    r = vision.detect(frame)
    check("HSV 识别 red_block", r is not None and r.material == "red_block",
          f"got {r.to_dict() if r else None}")
    check("HSV 置信度>=0.5", r is not None and r.confidence >= 0.5,
          f"got {r.confidence}" if r else "None")
    base_div = s2c.diverted_total
    base_vd = gw.stats.vision["dispatched"]
    pub("sort/inbound/vision", r.to_dict())
    ok = wait_for(lambda: s2c.diverted_total == base_div + 1
                  and gw.stats.vision["dispatched"] == base_vd + 1,
                  8, "视觉帧 -> divert 2 号分拣器")
    check("视觉帧 -> 2 号分拣器 divert", ok,
          f"diverted={s2c.diverted_total} disp={gw.stats.vision['dispatched']}")

    pc.loop_stop(); pc.disconnect()
    gw.stop()

    # ---------- 汇总 ----------
    print(f"\n{'=' * 46}\nPASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    BPORT = GPORT = 0
    main()
