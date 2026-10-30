#!/usr/bin/env python3
"""真机虚拟光门 E2E (MQTT 直发设备, 压缩时序):

1. 起带 (sort/line1/conveyor/cmd start, v=1.0)
2. 串口注入锚点 vision red_apple (T0, p0=0)
3. T0+150ms 发 divert (trigger=photo) -> 固件 match 命中 (150ms < 3s 窗,
   virtAt=T0+500ms 在未来) -> armed via=virtual, 500ms 到点推出
4. 收 sorter ack + /motion 时间线 (网关侧, 验证 relay 路径)
5. 停带
"""
import json
import time
import urllib.request

import paho.mqtt.client as paho
import serial

BROKER = "tcp://127.0.0.1:1883"
GW = "http://127.0.0.1:8123"
JID = "vgt-live-2"
MAT = "test_item"  # 网关白名单外: 网关不会自动分发, 避免与直发 job 撞车

acks = {}
client = paho.Client(client_id="vgt-test")
client.on_message = lambda c, u, m: acks.setdefault(m.topic, []).append(m.payload.decode())
client.connect("127.0.0.1", 1883)
for _t in ("sort/line1/motion", f"sort/sorter1/ack/{JID}",
           "sort/line1/conveyor/ack/vgt-start"):
    client.subscribe(_t)
client.loop_start()


def pub(topic, obj, qos=1):
    client.publish(topic, json.dumps(obj), qos=qos)


def http(path):
    with urllib.request.urlopen(GW + path, timeout=5) as r:
        return json.loads(r.read().decode())


s = serial.Serial("/dev/ttyACM0", 115200, timeout=0.2)
time.sleep(0.8)
s.reset_input_buffer()


def read_serial(ms):
    time.sleep(ms / 1000)
    out = ""
    while s.in_waiting:
        out += s.read(s.in_waiting).decode("utf-8", "replace")
        time.sleep(0.03)
    return out


log = ""

# 1. 起带
pub("sort/line1/conveyor/cmd", {"job_id": "vgt-start", "action": "start",
                                 "speed_mps": 1.0})
time.sleep(0.4)
log += read_serial(300)
assert acks.get("sort/line1/conveyor/ack/vgt-start"), "conveyor start 无 ack"
print("1) conveyor start ack:", acks["sort/line1/conveyor/ack/vgt-start"][0])

# 2. 注入锚点 (T0). CDC 偶发丢字节: 用板子回显 "vision <mat> published"
#    校验实际解析到的 material, 不符则重试 (锚点同物料覆盖, 最新一次生效)
t0 = time.time()
anchor_ok = False
buf = ""
for _attempt in range(6):
    s.reset_input_buffer()
    # 对齐 cdc_probe P1 的干净模式: 无前导 \n, vision 后两个空格, 单次写
    s.write(b"vision  " + MAT.encode() + b"\n")
    time.sleep(0.25)
    buf = ""
    while s.in_waiting:
        buf += s.read(s.in_waiting).decode("utf-8", "replace")
        time.sleep(0.02)
    if f"vision {MAT} published" in buf:
        anchor_ok = True
        break
    print(f"   锚点校验失败 (回显: {[l for l in buf.splitlines() if 'published' in l]}), 重试")
    log += buf
    time.sleep(0.15)
assert anchor_ok, "锚点注入始终校验失败 (CDC 丢字节)"
log += buf
print(f"2) 锚点注入确认 T0={t0:.3f}")

# 3. 尽快发 divert (photo); 锚点须足够新鲜: 预测 T0+500ms 须在未来
pub("sort/sorter1/cmd", {"job_id": JID, "action": "divert",
                         "material": MAT, "qty": 1,
                         "trigger": "photo"})
print(f"3) divert T+{(time.time()-t0)*1000:.0f}ms (trigger=photo)")

# 4. 等 ack (虚拟光门: ~500ms 预测 + 400ms 扫掠 ≈ 1s)
for _ in range(30):
    if f"sort/sorter1/ack/{JID}" in acks:
        break
    time.sleep(0.2)
    s.reset_input_buffer()
    log += read_serial(150)
ack = acks.get(f"sort/sorter1/ack/{JID}", [None])[0]
print("4) divert ack:", ack)

# 5. 停带
pub("sort/line1/conveyor/cmd", {"job_id": "vgt-stop", "action": "stop"})
log += read_serial(400)

# 6. 网关侧 /motion (relay 路径)
m = http("/motion")
print(f"\n=== /motion (count={m['count']}) ===")
for e in m["motion"]:
    print(json.dumps(e, ensure_ascii=False))

print("\n=== 串口关键日志 ===")
for line in log.splitlines():
    if any(k in line for k in ("[vgate]", "[gate]", "vision test_item",
                               "publish", "track")):
        print(line.strip())
s.close()
client.loop_stop()
