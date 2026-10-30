#!/usr/bin/env python3
"""探针: 不同串口发送方式下, 板子实际解析到的 material (经 'vision X published' 回显)."""
import time

import serial

s = serial.Serial("/dev/ttyACM0", 115200, timeout=0.2)
time.sleep(1.0)


def send_and_read(raw_chunks, pauses_ms=(0,)):
    """raw_chunks: list[bytes] 依次写入; pauses: 写完后等 N ms 再读回显."""
    s.reset_input_buffer()
    for i, c in enumerate(raw_chunks):
        s.write(c)
        if i + 1 < len(raw_chunks):
            time.sleep(0.05)
    time.sleep((pauses_ms[0] if pauses_ms else 300) / 1000)
    out = ""
    while s.in_waiting:
        out += s.read(s.in_waiting).decode("utf-8", "replace")
        time.sleep(0.02)
    for line in out.splitlines():
        if "published" in line:
            return line.strip()
    return "(无回显) " + out.replace("\n", " | ")[:120]


tests = [
    ("P1 单次19B无\\n", [b"vision  1111111111\n"], (300,)),
    ("P2 单次20B带\\n", [b"\nvision  1111111111\n"], (300,)),
    ("P3 两次写 9+11", [b"\nvision  ", b"1111111111\n"], (300,)),
    ("P4 两次写 带停顿", [b"\nvision  ", b"1111111111\n"], (0,)),
    ("P5 逐字节", [bytes([b]) for b in b"vision  2222222222\n"], (400,)),
    ("P6 两次写 短材料", [b"\nvision  ", b"ab12\n"], (300,)),
]
# P4 需要中间停顿: 单独处理
for name, chunks, pauses in tests:
    if name.startswith("P4"):
        s.reset_input_buffer()
        s.write(chunks[0])
        time.sleep(0.3)
        s.write(chunks[1])
        time.sleep(0.3)
        out = ""
        while s.in_waiting:
            out += s.read(s.in_waiting).decode("utf-8", "replace")
            time.sleep(0.02)
        got = next((l.strip() for l in out.splitlines() if "published" in l),
                   "(无回显) " + out[:100])
    else:
        got = send_and_read(chunks, pauses)
    print(f"{name:18s} -> {got}")
    time.sleep(0.3)
s.close()
