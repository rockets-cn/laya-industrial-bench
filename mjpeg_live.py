#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MJPEG 连续流服务 (浏览器可直接看)。

硬件曝光/亮度指令这相机无效, 所以用软件降亮度对抗过曝。
- /stream?b=0.6   连续 MJPEG 流; b=亮度系数(0.1~1.5, 越小越暗, 默认 0.6)
- /              单帧 JPEG (方便 curl 抓一帧)

默认端口 8125, 绑 0.0.0.0 (局域网可访问)。
"""
import cv2, time, threading, os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse

PORT = int(os.environ.get("MJPEG_PORT", "8125"))
CAP_INDEX = 0
W, H = 640, 480
DEFAULT_BRIGHT = 0.6          # 软件降亮度默认系数 (0.6 -> 200 灰度压到 ~120)
FPS_TARGET = 25               # 抓帧节流

class Cam:
    def __init__(self):
        self.cap = cv2.VideoCapture(CAP_INDEX)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
        time.sleep(0.8)
        try: self.cap.set(cv2.CAP_PROP_EXPOSURE, -15)   # 尽力而为, 无效也无所谓
        except Exception: pass
        self.lock = threading.Lock()
    def frame(self, b):
        with self.lock:
            ok, fr = self.cap.read()
        if not ok or fr is None:
            return None
        if abs(b - 1.0) > 1e-3:
            fr = (fr.astype("float32") * b).astype("uint8")
        return fr

cam = Cam()
BULLET = b"\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n"

class Handler(BaseHTTPRequestHandler):
    def _b(self, path):
        q = urllib.parse.urlparse(path).query
        b = DEFAULT_BRIGHT
        for kv in q.split("&"):
            if kv.startswith("b="):
                try: b = max(0.1, min(1.5, float(kv[2:])))
                except Exception: pass
        return b
    def do_GET(self):
        if self.path.split("?")[0] == "/stream":
            b = self._b(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    t0 = time.time()
                    fr = cam.frame(b)
                    if fr is None:
                        time.sleep(0.2); continue
                    ok, jpg = cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        self.wfile.write(BULLET + jpg.tobytes())
                        self.wfile.flush()
                    dt = time.time() - t0
                    if dt < 1.0 / FPS_TARGET:
                        time.sleep(1.0 / FPS_TARGET - dt)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            fr = cam.frame(self._b(self.path))
            if fr is None:
                self.send_error(500, "no frame"); return
            ok, jpg = cv2.imencode(".jpg", fr)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(jpg.tobytes())
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"MJPEG 流: http://0.0.0.0:{PORT}/stream?b=0.6  (软件降亮度, 默认 b=0.6)")
    srv.serve_forever()
