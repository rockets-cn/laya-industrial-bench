#!/usr/bin/env python3
"""真机网关驱动路径: 串口注入 vision (白名单物料) -> 网关自动 photo 分发
-> 固件虚拟光门预测 -> 推杆. 验证 /motion 时间线 via=virtual.
"""
import json
import time
import urllib.request

import serial

GW = "http://127.0.0.1:8123"
MAT = "red_apple"  # 网关白名单 -> 1 号分拣器


def http(path):
    with urllib.request.urlopen(GW + path, timeout=5) as r:
        return json.loads(r.read().decode())


base_count = http("/motion")["count"]
print(f"起始 /motion count={base_count}")

# 起带 (直发 MQTT 免网关往返): 直接调网关 /dispatch 会先起带, 但我们要先
# 让带跑着再注入锚点 (预测需要 v>0). 用 paho 直发.
import paho.mqtt.client as paho
c = paho.Client(client_id="vgt-gw-test")
c.connect("127.0.0.1", 1883)
c.loop_start()
c.publish("sort/line1/conveyor/cmd",
          json.dumps({"job_id": "vgt-gw-start", "action": "start",
                      "speed_mps": 1.0}), qos=1)
time.sleep(0.5)

s = serial.Serial("/dev/ttyACM0", 115200, timeout=0.2)
time.sleep(0.8)

# 注入锚点 (网关会同时收到 vision 事件 -> 自动 dispatch, trigger=photo)
t0 = time.time()
ok = False
for _ in range(6):
    s.reset_input_buffer()
    s.write(b"vision  " + MAT.encode() + b"\n")
    time.sleep(0.25)
    buf = ""
    while s.in_waiting:
        buf += s.read(s.in_waiting).decode("utf-8", "replace")
        time.sleep(0.02)
    if f"vision {MAT} published" in buf:
        ok = True
        break
    print("锚点校验失败, 重试:", [l for l in buf.splitlines() if "published" in l])
    time.sleep(0.15)
assert ok, "锚点注入失败"
print(f"T0={t0:.3f} 锚点注入确认 (网关应已自动分发)")

# 等 job 完成: 网关 batch 结束会停带; 轮询 /motion 新增 armed(virtual)
deadline = time.time() + 15
new_events = []
while time.time() < deadline:
    time.sleep(0.3)
    m = http("/motion")
    new_events = m["motion"][base_count:]
    if any(e.get("via") in ("virtual", "immediate", "photo")
           and e.get("state") in ("busy", "fault") for e in new_events):
        break

time.sleep(1.2)  # 等 ready(done) 入账
m = http("/motion")
new_events = m["motion"][base_count:]
print(f"\n=== /motion 新增 {len(new_events)} 条 ===")
for e in new_events:
    print(json.dumps(e, ensure_ascii=False))

st = http("/stats")
print("\nstats: dispatched =", st.get("items_dispatched"),
      "jobs =", st.get("jobs_done"), "/", st.get("jobs_faulted") if "jobs_faulted" in st else "")

c.loop_stop()
s.close()
