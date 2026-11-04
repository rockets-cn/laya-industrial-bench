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
 8. divert 时序回归: 超时随 qty 缩放 / busy-ack 延长等待 / 不重复拨料
 9. 光门触发: divert trigger=photo (armed->光门->拨出) / 视觉坐标透传 /
   photo 超时 fault 锁存
 10. 运动事件时间线: 设备状态迁移 sort/line1/motion -> 网关 GET /motion
   + relay sort/event/motion / 视觉 pos_mm (带面位置) 透传批次事件
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


def make_args(no_ack=False, item_ms=120):
    class A:  # 模拟 CLI 参数
        pass
    a = A()
    a.broker_host, a.broker_port = "127.0.0.1", BPORT
    a.latency_ms, a.fault_rate, a.no_ack = 20, 0.0, no_ack
    a.item_ms = item_ms
    a.belt_fault = None
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
           "divert_timeout_per_item_s": 1.0, "busy_wait_s": 3.0,
           "busy_wait_max_ext": 3,
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
    # 实际看到的: 校验通过的视觉事件进环形缓冲, GET /vision 供数字孪生按实显示
    # (未知物料/低置信不记账 — 它们不是"确认看到的", 避免孪生显示误检)
    st, body = http_json(f"http://127.0.0.1:{GPORT}/vision")
    vis = body.get("vision", [])
    check("GET /vision 200 + 仅记校验通过事件",
          st == 200 and body.get("count") == 1 and len(vis) == 1, f"got {st} {body}")
    if vis:
        check("/vision 事件内容 (物料/去向/置信度/时间)",
              vis[0]["material"] == "red_block" and vis[0]["confidence"] == 0.9
              and vis[0]["yard"] in (1, 2, 3) and "ts" in vis[0],
              f"got {vis[0]}")

    # ---------- 4. SCADA 入站命令 ----------
    print("\n== 4. SCADA 入站 sort/inbound/command ==")
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

    # ---------- 8. divert 时序回归 (实机: ~0.9s/件, 旧固定 3s 超时必炸) ----------
    print("\n== 8. divert 时序回归 (实机时序) ==")
    saved_cfg = {k: gw.cfg.get(k) for k in ("ack_timeout_s", "ack_retries",
                                            "busy_wait_s")}
    try:
        # 8a. 超时随 qty 缩放: 4 件 × 900ms = 3.6s > 旧固定 3.0s;
        #     新超时 = 3.0 + 1.0×4 = 7.0s -> 应成功, 且设备不得重复拨料
        gw.cfg["ack_timeout_s"], gw.cfg["ack_retries"] = 3.0, 2
        s1.item_ms = 900
        base8, base8d = gw.stats.batches["done"], s1.diverted_total
        st, _ = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                          {"source": "test",
                           "actions": [{"yard": 1, "material": "red_apple", "qty": 4}]})
        ok = wait_for(lambda: s1.diverted_total == base8d + 4, 15,
                      "拨 4 件 (实际 3.6s)")
        check("qty=4 超旧 3s 超时 -> 现在成功", ok and st == 202, f"st={st}")
        time.sleep(0.3)
        check("批次入账 done", gw.stats.batches["done"] > base8)
        check("拨 4 件 (未重复拨)", s1.diverted_total == base8d + 4,
              f"got +{s1.diverted_total - base8d}")

        # 8b. 设备慢于首个超时窗口 -> 重发撞 busy -> busy-ack 延长等待 (不重发),
        #     迟到的 ok-ack 仍入账
        gw.cfg["ack_timeout_s"], gw.cfg["busy_wait_s"] = 1.5, 1.0
        s2c.item_ms = 4000
        base9, base9d = gw.stats.batches["done"], s2c.diverted_total
        st, _ = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                          {"source": "test",
                           "actions": [{"yard": 2, "material": "banana", "qty": 1}]})
        ok = wait_for(lambda: s2c.diverted_total == base9d + 1, 15,
                      "busy 延长后完成")
        check("设备慢 (4s) + 重发撞 busy -> 仍完成", ok and st == 202, f"st={st}")
        time.sleep(0.3)
        check("批次入账 done", gw.stats.batches["done"] > base9)
        check("只拨 1 件 (busy/重发不重复拨)", s2c.diverted_total == base9d + 1,
              f"got +{s2c.diverted_total - base9d}")
    finally:
        for k, v in saved_cfg.items():
            if v is not None:
                gw.cfg[k] = v
            else:
                gw.cfg.pop(k, None)
        s1.item_ms = 120        # 恢复模拟设备默认节奏 (后续节依赖)
        s2c.item_ms = 120

    # ---------- 9. 光门触发回归 (divert trigger=photo + 视觉坐标透传) ----------
    # 物料选 banana: config/material_map.json 定稿映射 水果->3 号仓 (本节省时
    # 桩全打在 s3 上); yellow/blue 方块->1 号, 红/绿方块->2 号 (70b26db 定稿)
    print("\n== 9. 光门触发 (trigger=photo) ==")
    saved_cfg9 = {k: gw.cfg.get(k) for k in ("divert_trigger", "photo_margin_s",
                                             "ack_timeout_s")}
    batch_events = []

    def on_ev(c, u, msg):
        try:
            batch_events.append(json.loads(msg.payload.decode()))
        except Exception:
            pass

    evc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-ev")
    evc.on_message = on_ev
    evc.connect("127.0.0.1", BPORT)
    evc.subscribe("sort/event/batch", qos=1)
    evc.loop_start()
    time.sleep(0.3)
    try:
        gw.cfg["divert_trigger"] = "photo"
        gw.cfg["photo_margin_s"] = 1.0
        s3.photo_wait_s = 0.2     # 物品 相机->光门 行程
        s3.photo_timeout_s = 2.0  # 光门等待窗 (模拟固件 5s)

        # 9a. vision -> divert trigger=photo -> armed -> 光门 -> 拨出
        base10, base10d = gw.stats.batches["done"], s3.diverted_total
        seen_payloads = []
        orig_handle = s3.handle_cmd

        def spy_handle(payload):
            seen_payloads.append(payload)
            orig_handle(payload)

        s3.handle_cmd = spy_handle
        states_seen = set()

        def sample_states():
            end = time.time() + 1.5
            while time.time() < end:
                states_seen.add(s3.state)
                time.sleep(0.02)

        threading.Thread(target=sample_states, daemon=True).start()
        pub("sort/inbound/vision", {"material": "banana", "confidence": 0.9,
                                     "frame_id": "t-gate", "x": 120, "y": 80,
                                     "w": 40, "h": 40})
        ok = wait_for(lambda: s3.diverted_total == base10d + 1, 10,
                      "光门触发拨出")
        check("photo 触发 -> 拨出 1 件", ok, f"diverted={s3.diverted_total}")
        time.sleep(0.2)
        check("批次入账 done", gw.stats.batches["done"] > base10)
        check("命令携带 trigger=photo",
              any(p.get("trigger") == "photo" for p in seen_payloads),
              f"payloads={seen_payloads[:1]}")
        check("设备状态经过 armed", "armed" in states_seen,
              f"states={states_seen}")
        s3.handle_cmd = orig_handle

        # 9b. 视觉坐标 x/y/w/h 随批次事件透传 (上游标定/对账的原始数据)
        ok = wait_for(lambda: any(e.get("phase") in ("done", "fault")
                                  and (e.get("vision") or {}).get("x") == 120
                                  for e in batch_events), 8, "批次事件带 vision.x")
        ev = [e for e in batch_events if (e.get("vision") or {}).get("x") == 120]
        check("批次事件透传视觉坐标", ok and ev and ev[0]["vision"]["y"] == 80
              and ev[0]["vision"]["material"] == "banana",
              f"ev={ev[0] if ev else None}")

        # 9c. 光门无物品 (期望的件没到) -> 设备 fault -> 网关锁存
        s3.photo_dead = True
        base11f = gw.stats.batches["fault"]
        pub("sort/inbound/vision", {"material": "banana", "confidence": 0.9,
                                     "frame_id": "t-gate-2"})
        ok = wait_for(lambda: gw.stats.batches["fault"] > base11f, 12,
                      "photo 超时 -> 故障锁存")
        check("光门超时 -> fault 锁存", ok and gw.fault is not None,
              f"fault={gw.fault}")
        st, body = http_json(f"http://127.0.0.1:{GPORT}/fault/clear", {})
        check("photo fault 可人工清除", st == 200 and gw.fault is None)
        s3.photo_dead = False
        time.sleep(0.3)
    finally:
        for k, v in saved_cfg9.items():
            if v is not None:
                gw.cfg[k] = v
            else:
                gw.cfg.pop(k, None)
    evc.loop_stop(); evc.disconnect()

    # ---------- 10. 运动事件时间线 (sort/line1/motion -> GET /motion + relay) ----------
    print("\n== 10. 运动事件时间线 (/motion) ==")
    motion_events = []
    batch_events10 = []

    def on_motion(c, u, msg):
        try:
            motion_events.append(json.loads(msg.payload.decode()))
        except Exception:
            pass

    def on_batch10(c, u, msg):
        try:
            batch_events10.append(json.loads(msg.payload.decode()))
        except Exception:
            pass

    mc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-motion")
    mc.on_message = lambda c, u, msg: (
        on_motion(c, u, msg) if msg.topic == "sort/event/motion"
        else on_batch10(c, u, msg))
    mc.connect("127.0.0.1", BPORT)
    mc.subscribe("sort/event/motion", qos=0)
    mc.subscribe("sort/event/batch", qos=1)
    mc.loop_start()
    time.sleep(0.3)
    try:
        s2c.item_ms = 120   # 确保 2 号分拣器按默认节奏 (防前节污染)
        st, body = http_json(f"http://127.0.0.1:{GPORT}/motion")
        check("GET /motion 可用", st == 200 and "motion" in body and "count" in body,
              f"st={st} body={str(body)[:80]}")
        n_before = body.get("count", 0)
        base12 = gw.stats.batches["done"]
        # red_block -> 2 号分拣器 (s2c), immediate 时序: busy(via=immediate) -> done
        pub("sort/inbound/vision", {"material": "red_block", "confidence": 0.9,
                                    "frame_id": "t-motion", "x": 200, "y": 90,
                                    "w": 40, "h": 30, "pos_mm": 25.5})
        ok = wait_for(lambda: gw.stats.batches["done"] > base12, 10, "批次 done")
        check("视觉批次 done", ok)
        time.sleep(0.5)   # 等运动事件入时间线 (QoS0, 异步)
        st, body = http_json(f"http://127.0.0.1:{GPORT}/motion")
        check("时间线有新增", st == 200 and body["count"] > n_before,
              f"count={body.get('count')} before={n_before}")
        evs = [e for e in body.get("motion", []) if e.get("sorter") == 2]
        check("immediate 运动: busy(via=immediate)",
              any(e["via"] == "immediate" and e["state"] == "busy" for e in evs),
              f"evs={evs[-3:]}")
        check("完成运动: ready(via=done)",
              any(e["via"] == "done" and e["state"] == "ready" for e in evs))
        check("运动事件含 ts/job_id",
              all("ts" in e and "job_id" in e for e in body.get("motion", [])))
        ok = wait_for(lambda: any(e.get("via") == "done" for e in motion_events),
                      8, "relay sort/event/motion")
        check("relay sort/event/motion", ok, f"events={motion_events[-3:]}")
        # pos_mm 透传: 视觉事件带带面位置进批次事件 (上游标定/对账原始数据)
        ok = wait_for(lambda: any((e.get("vision") or {}).get("pos_mm") == 25.5
                                  and e.get("phase") in ("done", "fault")
                                  for e in batch_events10), 8, "pos_mm 透传")
        check("视觉 pos_mm 透传批次事件", ok,
              f"ev={batch_events10[-1] if batch_events10 else None}")

        # ---------- 11. 带健康故障链路 (conveyor 遥测 fault -> 网关锁存) ----------
        print("\n== 11. 带健康故障链路 (sim 注入 belt-slip/belt-jam) ==")
        base13 = gw.stats.batches["done"]
        conv.belt_fault = "jam"  # 带子一 running 就注入 (items_in 早已 >= 2)
        # 直接起带 (in-process handler, 绕过 job 短窗口): 遥测周期 0.5s, 若 job 的
        # start→stop (~300ms) 恰好落在两次 tick 之间, running tick 被错过 → flaky.
        # 注意: 首个 running tick 内 belt_fault 即触发, 网关侧 dev_state 只见 fault
        conv.handle_cmd({"action": "start", "job_id": "belt-watch-test",
                         "speed_mps": 1.0})
        check("注入前置: 带已起且 items_in>=2",
              conv.state == "running" and conv.items_in >= 2,
              f"conv={conv.state} items_in={conv.items_in}")
        ok = wait_for(lambda: gw.dev_state.get("conveyor") == "fault",
                      8, "conveyor 遥测 fault")
        check("conveyor 遥测 fault (sim 注入)", ok, f"dev={gw.dev_state}")
        ok = wait_for(lambda: gw.fault
                      and gw.fault.get("device") == "conveyor", 5, "网关锁存")
        check("网关故障锁存 (device=conveyor)", ok, f"fault={gw.fault}")
        check("锁存 note 含带健康原因",
              gw.fault and "belt" in (gw.fault.get("error") or ""),
              f"fault={gw.fault}")
        st, body = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                             {"source": "test",
                              "actions": [{"yard": 1, "material": "banana",
                                           "qty": 1}]})
        check("锁存期 dispatch 409", st == 409, f"st={st} body={body}")
        st, body = http_json(f"http://127.0.0.1:{GPORT}/health")
        check("/health 报 conveyor fault + 带健康 note",
              st == 200 and body.get("conveyor") == "fault"
              and "belt" in (body.get("conveyor_note") or ""),
              f"body={body}")
        st, body = http_json(f"http://127.0.0.1:{GPORT}/fault/clear", {})
        check("人工清除 200", st == 200 and body.get("cleared"),
              f"st={st} body={body}")
        check("fault 已清除", gw.fault is None)
        conv.belt_fault = ""  # 不再注入; 清除时网关已尽力向设备发 reset
        ok = wait_for(lambda: gw.dev_state.get("conveyor") == "stopped",
                      5, "设备侧 reset 回 stopped")
        check("清除后设备回 stopped (reset 下发)", ok, f"dev={gw.dev_state}")
        st, body = http_json(f"http://127.0.0.1:{GPORT}/dispatch",
                             {"source": "test",
                              "actions": [{"yard": 1, "material": "red_apple",
                                           "qty": 2}]})
        check("清除后 dispatch 202", st == 202, f"st={st} body={body}")
        ok = wait_for(lambda: gw.stats.batches["done"] > base13, 10,
                      "恢复后批次 done")
        check("恢复后批次 done", ok)
    finally:
        mc.loop_stop(); mc.disconnect()

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
