#!/usr/bin/env python3
"""串口冒烟: 读 boot 日志, 发 state/track/gates, 回读."""
import serial, time, sys

PORT = "/dev/ttyACM0"
Baud = 115200

def drain(s, ms):
    time.sleep(ms / 1000)
    out = ""
    while s.in_waiting:
        out += s.read(s.in_waiting).decode("utf-8", "replace")
        time.sleep(0.05)
    return out

s = serial.Serial(PORT, Baud, timeout=0.2)
time.sleep(1.5)          # 板子刚 hard reset, 等 boot 完
s.reset_input_buffer()
# 主动软重启一次, 抓完整 boot 日志 (含 [vgate] 虚拟光门状态)
s.write(b"reboot\n")
time.sleep(4.0)
boot = drain(s, 2500)
s.reset_input_buffer()
s.write(b"state\n")
time.sleep(0.8)
r1 = drain(s, 500)
s.write(b"track\n")
time.sleep(0.8)
r2 = drain(s, 500)
s.write(b"gates\n")
time.sleep(0.5)
r3 = drain(s, 400)
print("=== boot ===")
print(boot)
print("=== state ===")
print(r1)
print("=== track ===")
print(r2)
print("=== gates ===")
print(r3)
s.close()
