"""cam_status.py — 实时画面状态监视 (widget 数据源).

取帧 (127.0.0.1:8125/stream, 带 X-Timestamp 判龄) -> YOLO 香蕉检测
-> 写 /tmp/cam_live/status.json (由 mjpeg_server /status 端点服务, widget 轮询).
皮带状态: 轮询网关 :8123/health; 推杆状态: MQTT sort/sorter{1,2,3}/telemetry.
不占串口 (与 beltctl 无冲突), CPU 版 YOLO ~1fps.
"""
import json
import os
import time
import urllib.request

import cv2  # noqa: F401  (保留, 便于扩展)
import numpy as np
import paho.mqtt.client as mqtt
import torch
torch.set_num_threads(4)   # 限制推理线程 (争核会烧 1.7+ 核, 2026-11-04 实测)
from ultralytics import YOLO

MODEL = '/tmp/banana_train/banana_v1-2/weights/best.pt'
OUT = '/tmp/cam_live/status.json'
STREAM = 'http://127.0.0.1:8125/stream'
GATEWAY = 'http://127.0.0.1:8123/health'

os.makedirs('/tmp/cam_live', exist_ok=True)
Y = YOLO(MODEL)

SER = mqtt.Client(client_id='cam_status', protocol=mqtt.MQTTv311)
SER.connect('127.0.0.1', 1883, 60)
S = {1: '?', 2: '?', 3: '?'}


def on_msg(c, u, m):
    for n in (1, 2, 3):
        if m.topic == f'sort/sorter{n}/telemetry':
            try:
                S[n] = json.loads(m.payload.decode()).get('state', '?')
            except Exception:
                pass


SER.on_message = on_msg
for _n in (1, 2, 3):
    SER.subscribe(f'sort/sorter{_n}/telemetry', qos=1)
SER.loop_start()


def grab(timeout=2.5):
    """从 MJPEG 流取一帧; 拒 X-Timestamp 龄 >0.6s 的旧帧."""
    try:
        resp = urllib.request.urlopen(urllib.request.Request(STREAM), timeout=10)
    except Exception:
        return None
    buf = b''
    end = time.time() + timeout
    while time.time() < end:
        try:
            chunk = resp.read(65536)
        except Exception:
            break
        if not chunk:
            continue
        buf += chunk
        a = buf.find(b'\xff\xd8')
        if a < 0:
            continue
        b = buf.find(b'\xff\xd9', a + 2)
        if b >= 0:
            import re
            m = re.search(rb'X-Timestamp:\s*(\d+)', buf[:a])
            if m:
                age = time.time() * 1000 - int(m.group(1))
                if age > 600:
                    continue  # 旧帧, 继续等
            f = cv2.imdecode(np.frombuffer(buf[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
            if f is not None:
                return f
    return None


last_belt = '?'
last_health_ts = 0.0
while True:
    t0 = time.time()
    if time.time() - last_health_ts > 2.0:
        try:
            with urllib.request.urlopen(GATEWAY, timeout=3) as r:
                last_belt = json.loads(r.read()).get('conveyor', '?')
            last_health_ts = time.time()
        except Exception:
            pass
    boxes = []
    f = grab()
    if f is not None:
        try:
            r = Y(f, conf=0.2, iou=0.5, device='cpu', verbose=False)[0]
            if r.boxes is not None:
                for bx in r.boxes:
                    x1, y1, x2, y2 = [int(v) for v in bx.xyxy[0]]
                    boxes.append({'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                                  'cx': round((x1 + x2) / 2), 'cy': round((y1 + y2) / 2),
                                  'conf': round(float(bx.conf[0]), 2)})
        except Exception:
            pass
    st = {'ts': time.time(), 'belt': last_belt,
          's1': S[1], 's2': S[2], 's3': S[3], 'bananas': boxes}
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(st, fh)
    os.replace(tmp, OUT)
    time.sleep(max(0.15, 2.0 - (time.time() - t0)))   # 2s 周期 (widget 数据粒度足够): 与 beltctl YOLO 错开争核窗口 (2026-11-04)
