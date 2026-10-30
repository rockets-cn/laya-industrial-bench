#!/usr/bin/env python3
"""诊断: 注入锚点后查 track / 带速."""
import time

import serial

s = serial.Serial("/dev/ttyACM0", 115200, timeout=0.2)
time.sleep(0.8)
s.reset_input_buffer()


def cmd(line, wait=600):
    s.reset_input_buffer()
    s.write(line.encode() + b"\n")
    time.sleep(wait / 1000)
    out = ""
    while s.in_waiting:
        out += s.read(s.in_waiting).decode("utf-8", "replace")
        time.sleep(0.03)
    return out.strip()


print("1) vision 注入:", cmd("vision red_apple"))
print("2) track:", cmd("track"))
print("3) state:", cmd("state"))
print("4) 再注入一次 + 立即 track:", cmd("vision red_apple", 200) + " | " + cmd("track"))
s.close()
