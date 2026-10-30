#!/usr/bin/env python3
"""真机虚拟光门 E2E: 串口注入锚点 -> /dispatch -> 收 /motion 时间线.

时序: T0 注入 vision red_apple (锚点, p0=0) -> T0+0.2s POST /dispatch
(网关自动 start conveyor, divert trigger=photo; 固件 match 命中 ->
armed, 预测 T0+500ms 过光门线 -> 到点推杆 via=virtual) -> 轮询 /motion.
"""
import json
import time
import urllib.request

import serial

GW = "http://127.0.0.1:8123"


def http(path, data=None):
    req = urllib.request.Request(GW + path, method="POST" if data else "GET")
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, body, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


s = serial.Serial("/dev/ttyACM0", 115200, timeout=0.2)
time.sleep(0.8)
s.reset_input_buffer()

t0 = time.time()
s.write(b"vision red_apple\n")
time.sleep(0.2)
status, dispatch = http("/dispatch", {"actions": [{"yard": 1, "material": "red_apple", "qty": 1}], "source": "vgate-test"})
print(f"T+{(time.time()-t0)*1000:.0f}ms dispatch -> {status} {dispatch}")

serial_log = ""
m = {"count": 0, "motion": []}
deadline = time.time() + 12
while time.time() < deadline:
    time.sleep(0.3)
    while s.in_waiting:
        serial_log += s.read(s.in_waiting).decode("utf-8", "replace")
    _, m = http("/motion")
    if m["count"] >= 3:  # armed -> busy(via) -> ready(done)
        break

print(f"\n=== /motion (count={m['count']}) ===")
for e in m["motion"]:
    print(e)
print("\n=== serial (关键行) ===")
for line in serial_log.splitlines():
    if any(k in line for k in ("[vgate]", "[divert]", "motion", "divert", "ok", "busy", "fault", "armed")):
        print(line.strip())
s.close()
