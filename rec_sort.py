#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录分拣全程视频 + 同步跑一次 divert(1号仓) + 抓串口日志。

从 MJPEG 流(8125)拉帧录, 不抢 /dev/video0 (mjpeg_live.py 仍占着相机), 也不打断孪生。
用法: rec_sort.py <out.mp4> [秒数=13] [b=0.7]
录像 t=1.0s 时 armed(MQTT) + 串口 belt start 0.0525, 完整覆盖 过IR->外推->瞬停->推。
"""
import cv2, sys, time, json, os, numpy as np, socket
import serial
import paho.mqtt.client as mqtt
from urllib.parse import urlparse

PORT = "/dev/ttyACM0"
MJPEG = "http://127.0.0.1:8125/stream?b={b}"

def open_stream(b):
    u = urlparse(MJPEG.format(b=b))
    host, port = u.hostname, u.port or 80
    path = u.path + ("?" + u.query if u.query else "")
    s = socket.create_connection((host, port), timeout=30)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n".encode())
    return s

def next_frame(sock):
    """阻塞读到下一帧 JPEG, 返回 (img, 耗时s). 失败返回 (None, dt). buf 是 bytes(不可变), 用切片丢前缀."""
    buf = getattr(next_frame, "buf", b"")
    t0 = time.time()
    while True:
        i = buf.find(b"\xff\xd8")
        if i >= 0:
            j = buf.find(b"\xff\xd9", i + 2)
            if j >= 0:
                jpeg = buf[i:j+2]
                next_frame.buf = buf[j+2:]
                fr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if fr is not None and fr.ndim == 2:      # 灰度/异常帧 -> 转 BGR
                    fr = cv2.cvtColor(fr, cv2.COLOR_GRAY2BGR)
                return fr, time.time() - t0
            buf = buf[i:]
        else:
            buf = buf[-3:] if len(buf) > 3 else buf
        try:
            chunk = sock.recv(8192)
        except Exception:
            next_frame.buf = buf
            return None, time.time() - t0
        if not chunk:
            next_frame.buf = buf
            return None, time.time() - t0
        buf += chunk

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sort_run.mp4"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 13.0
    b = float(sys.argv[3]) if len(sys.argv) > 3 else 0.7
    BIN = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    W, H, FPS = 640, 480, 20

    sock = open_stream(b)
    next_frame.buf = b""
    fr0 = None
    for _ in range(15):
        fr0, _ = next_frame(sock)
        if fr0 is not None and fr0.ndim == 3:
            break
    if fr0 is None or fr0.ndim != 3:
        print("!! 无有效彩色帧 (MJPEG 流异常)"); sys.exit(2)
    print(f"[rec] 流 OK, 首帧 shape={fr0.shape}, 均值 {int(cv2.cvtColor(fr0, cv2.COLOR_BGR2GRAY).mean())}")

    writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    s = serial.Serial(PORT, 115200, timeout=0.3)
    for _ in range(5): s.read(4096); time.sleep(0.1)
    c = mqtt.Client(); c.connect("127.0.0.1", 1883); c.loop_start(); time.sleep(0.6)

    JOB = "rec_" + time.strftime("%H%M%S")
    d_bin = 125 + (BIN - 1) * 52  # d1=125(1号仓标定) + spacing
    t0 = time.time(); n = 0; fired = False; log = []
    print(f"[rec] 开录 {out} ({seconds:.0f}s) -> {BIN}号仓 (外推 d={d_bin}mm), JOB={JOB}, t=1.0s 起 armed+起带")
    while time.time() - t0 < seconds:
        fr, _ = next_frame(sock)
        if fr is not None:
            writer.write(fr); n += 1
        # 串口日志 (非阻塞读)
        iw = s.in_waiting
        if iw: log.append(f"{time.time()-t0:5.2f} {s.read(iw).decode('utf-8','replace').strip()}")
        if not fired and time.time()-t0 >= 1.0:
            fired = True
            c.publish(f"sort/sorter{BIN}/cmd", json.dumps(
                {"job_id":JOB,"action":"divert","ts":time.time(),"material":"red_block","qty":1}))
            s.write(b"belt start 0.0525\n")
            log.append(f"{time.time()-t0:5.2f} >> ARMED+START -> {BIN}号仓 (t0+1.0s)")
    s.write(b"belt stop\n"); time.sleep(0.3)
    s.write(b"state\n"); time.sleep(0.35)
    iw = s.in_waiting
    if iw: log.append(f"(end) {s.read(iw).decode('utf-8','replace').strip()}")
    writer.release(); c.loop_stop(); s.close(); sock.close()
    print(f"[rec] 完成 {out}, {n} 帧, {n/FPS:.1f}s 有效视频")
    print("=== 串口日志 (关键行) ===")
    for l in log:
        if any(k in l for k in ("[ir]","[push]","[cmd]","belt:","ARMED","conveyor=","state:")):
            print(" ", l)

if __name__ == "__main__":
    main()
