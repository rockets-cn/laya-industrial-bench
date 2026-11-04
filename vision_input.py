"""视觉传感器接入: 帧 -> 物料分类 -> sort/inbound/vision (网关做映射与下发).

识别位置说明 (同 bench_colorblock.py 的结论): 像素层负责识别, Laya/网关只
做决策与下发. 本模块两条路径:

  1. HSVBlockVision (本进程): 复用 bench_colorblock 的像素层 HSV 阈值
     (numpy, ms 级), 识别 4 种颜色方块 -> material = {red,green,yellow,
     blue}_block. 帧来源: --gen (合成图, 离线测试) / --file / --camera N.
  2. 外部传感器直发: 若视觉传感器自带模型, 直接发 sort/inbound/vision
     (material 已定), 无需本模块. 网关只认物料白名单 + 置信度.

水果类物料 (红苹果/绿苹果/香蕉/梨) 本机无离线 CV 模型 (CPU 机器), 走外部
传感器标签或后续小模型 — 见 OPEN_QUESTIONS.md.

发出事件 (QoS1, topic sort/inbound/vision):
  {"material":"blue_block","confidence":0.97,"frame_id":"f-...","bbox":[x,y,w,h],
   "source":"hsv_block","ts":"..."}

Run:
  .venv/bin/python vision_input.py --gen                # 合成 4 色图各发一帧
  .venv/bin/python vision_input.py --file shot.png
  .venv/bin/python vision_input.py --camera 0 --interval-ms 250
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

import numpy as np
import cv2
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("bc", os.path.join(HERE, "bench_colorblock.py"))
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class VisionResult:
    __slots__ = ("material", "confidence", "bbox", "source", "frame_id")

    def __init__(self, material, confidence, bbox, source, frame_id):
        self.material = material
        self.confidence = confidence
        self.bbox = bbox          # [x, y, w, h] 或 None
        self.source = source
        self.frame_id = frame_id

    def to_dict(self):
        return {"material": self.material, "confidence": self.confidence,
                "bbox": self.bbox, "source": self.source,
                "frame_id": self.frame_id, "ts": _now()}


class HSVBlockVision:
    """像素层 HSV 识别 4 种颜色方块. 单物体现: 画面主色占比达标即报.

    与 bench_colorblock.detect 的差异: 这里按 "传送带上单个物料" 场景调参 —
    主色占比 >= frac_min 且明显强于次色才报 (单格背景图 frac≈0.25+)."""

    COLOR2MAT = {"red": "red_block", "green": "green_block",
                  "yellow": "yellow_block", "blue": "blue_block"}

    def __init__(self, frac_min=0.08, dom_ratio=2.0, scale=320):
        self.frac_min = frac_min
        self.dom_ratio = dom_ratio
        self.scale = scale
        self._seq = 0

    def detect(self, frame):
        """frame (H,W,3) uint8 -> VisionResult | None."""
        dt, found = bc.detect(frame, scale=self.scale, frac_min=0.01)
        self._seq += 1
        if not found or found[0]["frac"] < self.frac_min:
            return None
        top = found[0]
        second = found[1]["frac"] if len(found) > 1 else 0.0
        if top["frac"] < self.dom_ratio * max(second, 0.01):
            return None
        mat = self.COLOR2MAT.get(top["key"])
        if not mat:
            return None
        conf = min(1.0, top["frac"] / 0.25)
        return VisionResult(mat, round(conf, 3), None,
                              "hsv_block(%dms)" % round(dt * 1000),
                              "f%d-%d" % (int(time.time() * 1000), self._seq))


class SceneBlockVision:
    """真机摄像头场景 (代替 HuskyLens2): 皮带 ROI 内颜色方块识别 + 驻留沿去重.

    与 HSVBlockVision (单格合成图, 主色占比) 不同: 真实画面有机架/背景,
    按 HSV 连通域 blob 识别, 且只在 blob 出现 (驻留沿) 时上报一次 —
    同一物品持续在画面中不会重复下发 (对齐光门边沿语义).

    ROI 默认 y∈[55,140] (皮带带, 2026-11-03 标定; 机架固定小黄点在 y≈52
    被排除). 每颜色 H 范围: 红双段 (HSV 环状), 其余单段."""

    COLOR2MAT = {"red": "red_block", "green": "green_block",
                  "yellow": "yellow_block", "blue": "blue_block"}
    HSV_RANGES = {
        "red":    [((0, 120, 60), (10, 255, 255)),
                   ((165, 120, 60), (180, 255, 255))],
        "yellow": [((18, 120, 100), (38, 255, 255))],
        "green":  [((40, 90, 50), (85, 255, 255))],
        "blue":   [((95, 90, 50), (130, 255, 255))],
    }

    def __init__(self, roi=(55, 140, 0, 640), min_area=300,
                 clear_ms=2000.0):
        self.y0, self.y1, self.x0, self.x1 = roi
        self.min_area = min_area
        self.clear_ms = clear_ms
        self._last_present = {}   # color -> 最后一次出现的 time
        self._seq = 0

    def _blobs(self, hsv):
        """{color: (area, cx, cy, x, y, w, h)} — ROI 内连通域, 取每色最大域."""
        out = {}
        for color, ranges in self.HSV_RANGES.items():
            m = np.zeros(hsv.shape[:2], np.uint8)
            for lo, hi in ranges:
                m |= cv2.inRange(hsv, lo, hi)
            m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            n, _lab, stats, cents = cv2.connectedComponentsWithStats(m, 8)
            best = None
            for i in range(1, n):
                x, y, w, h, area = stats[i]
                if area < self.min_area:
                    continue
                cx, cy = cents[i]
                if not (self.x0 <= cx < self.x1 and self.y0 <= cy < self.y1):
                    continue
                if best is None or area > best[0]:
                    best = (area, cx, cy, x, y, w, h)
            if best:
                out[color] = best
        return out

    def detect(self, frame):
        """frame (H,W,3) BGR uint8 -> VisionResult | None (驻留沿才报)."""
        now = time.time() * 1000.0
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        blobs = self._blobs(hsv)
        reportable = []
        for color, b in blobs.items():
            area, cx, cy, x, y, w, h = b
            lp = self._last_present.get(color)
            if lp is None or now - lp > self.clear_ms:
                reportable.append((area, color, b))
            self._last_present[color] = now
        if not reportable:
            # 维护消失状态 (clear_ms 后移除, 允许再次上报)
            for c in list(self._last_present):
                if c not in blobs and now - self._last_present[c] > self.clear_ms:
                    del self._last_present[c]
            return None
        # 单物品策略: 只报最大域
        reportable.sort(key=lambda t: -t[0])
        area, color, (a, cx, cy, x, y, w, h) = reportable[0]
        self._seq += 1
        conf = round(min(1.0, 0.5 + a / 6000.0), 3)
        return VisionResult(self.COLOR2MAT[color], conf,
                             [int(x), int(y), int(w), int(h)],
                             "scene_cam",
                             "f%d-%d" % (int(time.time() * 1000), self._seq))


class YoloFruitVision(SceneBlockVision):
    """Yolo 水果识别 (代替 HSV 认不出的香蕉等). 数据面同流模式,
    检测器为 ultralytics YOLO (XPU/CPU), 驻留沿去重逻辑复用父类.
    单帧: 取 conf 最高框 -> {color:'banana'} -> 走 SceneBlockVision.detect 的去重."""

    COLOR2MAT = {"banana": "banana", "pear": "pear",
                  "red_apple": "red_apple", "green_apple": "green_apple"}

    def __init__(self, model_path, roi=(55, 140, 0, 640), min_area=300,
                 clear_ms=2000.0, conf=0.3, device="xpu", imgsz=640):
        super().__init__(roi=roi, min_area=min_area, clear_ms=clear_ms)
        from ultralytics import YOLO
        self._yolo = YOLO(model_path)
        self._conf = conf
        self._device = device
        self._imgsz = imgsz
        print(f"[vision] Yolo 模型 {model_path} device={device} conf>={conf} imgsz={imgsz}",
              flush=True)

    def detect(self, frame):
        now = time.time() * 1000.0
        res = self._yolo(frame, conf=self._conf, iou=0.5, imgsz=self._imgsz,
                         device=self._device, verbose=False)[0]
        blobs = {}   # color -> 该类最高 conf 的 box
        if res.boxes is not None and len(res.boxes):
            names = res.names
            for i in range(len(res.boxes)):
                cls = int(res.boxes.cls[i])
                color = names.get(cls, str(cls))
                if color not in self.COLOR2MAT:
                    continue
                conf_v = float(res.boxes.conf[i])
                if color in blobs and conf_v < blobs[color][0]:
                    continue
                x1, y1, x2, y2 = [float(v) for v in res.boxes.xyxy[i]]
                w, h = x2 - x1, y2 - y1
                area = float(w * h)
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                if area < self.min_area:
                    continue
                if not (self.x0 <= cx < self.x1 and self.y0 <= cy < self.y1):
                    continue
                blobs[color] = (conf_v, area, cx, cy, x1, y1, w, h)
        reportable = []
        for color, b in blobs.items():
            lp = self._last_present.get(color)
            if lp is None or now - lp > self.clear_ms:
                reportable.append((b[1], color, b))
            self._last_present[color] = now
        if not reportable:
            for c in list(self._last_present):
                if c not in blobs and now - self._last_present[c] > self.clear_ms:
                    del self._last_present[c]
            return None
        # 单物品策略: 每帧只报最大框 (皮带同时只允许一件料)
        reportable.sort(key=lambda t: -t[0])
        _a, color, (conf_v, _area, _cx, _cy, x, y, w, h) = reportable[0]
        self._seq += 1
        return VisionResult(self.COLOR2MAT[color], round(conf_v, 3),
                             [int(x), int(y), int(w), int(h)],
                             "yolo_cam",
                             "f%d-%d" % (int(time.time() * 1000), self._seq))

    @property
    def last_seen_ms(self):
        """Yolo 最近一次看到水果的时刻 (ms); 无 = 0."""
        return max(self._last_present.values()) if self._last_present else 0.0


class VisionFeed:
    """帧循环 -> 识别 -> 发 sort/inbound/vision."""

    def __init__(self, broker_host, broker_port, vision, interval_ms=250,
                 quiet=None):
        self.vision = vision
        self.interval = interval_ms / 1000.0
        self.quiet = quiet
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id="vision-feed")
        self.client.connect(broker_host, broker_port)
        self.client.loop_start()
        self.published = 0

    def publish_frame_result(self, result):
        if result is None:
            return False
        info = self.client.publish("sort/inbound/vision",
                                      json.dumps(result.to_dict()), qos=1)
        info.wait_for_publish(2)
        self.published += 1
        print(f"[vision] 发布 {result.material} conf={result.confidence} "
              f"id={result.frame_id}", flush=True)
        return True

    def run_frames(self, frames):
        for i, f in enumerate(frames):
            r = self.vision.detect(f)
            self.publish_frame_result(r)
            if i < len(frames) - 1 and self.interval:
                time.sleep(self.interval)

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


def load_frames(args):
    """按参数取帧: --gen 合成单色图 / --file 单图. (--camera 走流式, 见 main)"""
    if args.file:
        from PIL import Image
        return [np.asarray(Image.open(args.file).convert("RGB"))]
    # --gen: 合成图 (单色/双色/四色, 前两张适合单物料场景)
    frames = [f for _, f, _ in bc.make_test_frames()]
    return frames[:2] if args.gen_single else frames


def mjpeg_frames(url, min_interval=0.06):
    """MJPEG 直播流 -> BGR 帧生成器 (127.0.0.1:8125/stream, 与浏览器直播同源).
    单摄像头单 V4L2 源, 多 HTTP 客户端共用 mjpeg_server 的帧缓冲, 不抢设备."""
    import urllib.request
    req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req, timeout=10)
    buf = b""
    last = 0.0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            time.sleep(0.2)
            continue
        buf += chunk
        while True:
            a = buf.find(b"\xff\xd8")
            if a < 0:
                buf = buf[-8:]; break
            b = buf.find(b"\xff\xd9", a + 2)
            if b < 0:
                if len(buf) > 4_000_000:
                    buf = buf[-8:]
                break
            now = time.time()
            if now - last < min_interval:
                buf = buf[b + 2:]
                continue
            arr = np.frombuffer(buf[a:b + 2], np.uint8)
            f = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            buf = buf[b + 2:]
            if f is None:
                continue
            last = now
            yield f


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="tcp://127.0.0.1:1883")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--gen", action="store_true",
                     help="合成测试图 (默认; 离线可跑)")
    src.add_argument("--file", default=None, help="单张图片")
    src.add_argument("--camera", type=int, default=None, help="摄像头索引")
    src.add_argument("--stream", default=None,
                     help="MJPEG 直播流 URL (默认 http://127.0.0.1:8125/stream, "
                            "真机摄像头代替 HuskyLens2)")
    ap.add_argument("--interval-ms", type=int, default=250,
                    help="帧间隔 (ms)")
    ap.add_argument("--frac-min", type=float, default=0.08)
    ap.add_argument("--roi", default="55,140,0,640",
                    help="--stream 场景识别 ROI y0,y1,x0,x1 (默认皮带带)")
    ap.add_argument("--min-area", type=int, default=300,
                    help="--stream 场景 blob 最小面积 (px)")
    ap.add_argument("--clear-ms", type=int, default=2000,
                    help="--stream 驻留沿去重: 消失多久后可再次上报 (ms)")
    ap.add_argument("--yolo-model", default=None,
                    help="Yolo 水果模型 (.pt, 4 类: banana/pear/red_apple/green_apple); "
                         "与色块检测并行, 各自去重")
    ap.add_argument("--yolo-device", default="auto",
                    help="yolo 推理设备: auto (XPU 可用则用, 否则 CPU) | xpu | cpu")
    ap.add_argument("--yolo-imgsz", type=int, default=320,
                    help="yolo 推理尺寸 (320 快; 640 准)")
    ap.add_argument("--fruit-roi", default="55,400,0,640",
                    help="水果 Yolo ROI y0,y1,x0,x1 (比色块 ROI 宽: 水果/阴影占位更大)")
    ap.add_argument("--gen-single", action="store_true",
                    help="--gen 只发前两张单色图 (模拟单物料)")
    a = ap.parse_args()
    if not (a.file or a.camera is not None or a.stream):
        a.gen = True
    m = re.match(r"tcp://([^:/]+):(\d+)$", a.broker)
    if m:
        a.broker_host, a.broker_port = m.group(1), int(m.group(2))
    else:
        h, _, p = a.broker.partition(":")
        a.broker_host, a.broker_port = h or "127.0.0.1", int(p or 1883)
    return a


def main():
    args = parse_args()
    if args.stream:
        # 真机场景模式: 摄像头直播流 (代替 HuskyLens2) -> 皮带 ROI 识别
        # -> 驻留沿去重 -> sort/inbound/vision -> 网关自动下发.
        stream_url = args.stream or "http://127.0.0.1:8125/stream"
        roi = tuple(int(v) for v in args.roi.split(","))
        detectors = [SceneBlockVision(roi=roi, min_area=args.min_area,
                                      clear_ms=args.clear_ms)]
        if args.yolo_model:
            dev = args.yolo_device
            if dev == "auto":
                try:
                    import torch
                    dev = "xpu" if torch.xpu.is_available() else "cpu"
                except Exception:
                    dev = "cpu"
            fruit_roi = tuple(int(v) for v in args.fruit_roi.split(","))
            detectors.append(YoloFruitVision(
                args.yolo_model, roi=fruit_roi, min_area=args.min_area,
                clear_ms=args.clear_ms, device=dev,
                imgsz=args.yolo_imgsz))
        feed = VisionFeed(args.broker_host, args.broker_port, detectors[0])
        print(f"[vision] 流模式 {stream_url} ROI={roi} "
              f"min_area={args.min_area} clear={args.clear_ms}ms "
              f"检测器={len(detectors)} (Ctrl-C 停止)", flush=True)
        fr, t0 = 0, time.time()
        yolo = next((d for d in detectors if isinstance(d, YoloFruitVision)), None)
        yolo_warm = yolo is None          # Yolo 首次推理完成前不发任何事件 (预热间隙)
        last_pub = {}                     # material -> 上次发布时刻 (feed 级硬冷却)
        for f in mjpeg_frames(stream_url):
            fr += 1
            for det in detectors:
                r = det.detect(f)
                if det is yolo:
                    yolo_warm = True
                if not r:
                    continue
                if not yolo_warm:
                    continue
                # 水果在带时 (Yolo 3s 内见过), 抑制 HSV 色块报告:
                # 香蕉黄皮会被 HSV 误报成 yellow_block -> 错格
                if det is not yolo and yolo is not None and \
                        time.time() * 1000 - yolo.last_seen_ms < 3000:
                    continue
                # feed 级硬冷却: 同物料 5s 内只发一次 (单物品单推)
                lp = last_pub.get(r.material)
                if lp is not None and time.time() - lp < 5.0:
                    continue
                last_pub[r.material] = time.time()
                feed.publish_frame_result(r)
            if fr % 100 == 0:
                print(f"[vision] 心跳 {fr} 帧 "
                      f"{fr / (time.time() - t0):.1f}fps 已发布 {feed.published}",
                      flush=True)
        feed.stop()
        print(f"[vision] 完成, 共发布 {feed.published} 个事件", flush=True)
        return
    vision = HSVBlockVision(frac_min=args.frac_min)
    feed = VisionFeed(args.broker_host, args.broker_port, vision,
                       interval_ms=args.interval_ms)
    if args.camera is not None:
        import cv2
        cap = cv2.VideoCapture(args.camera)
        assert cap.isOpened(), f"打不开摄像头 {args.camera}"
        cap.read()  # 丢首帧
        print(f"[vision] 摄像头 {args.camera} 流式识别中 (Ctrl-C 停止)",
              flush=True)
        try:
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                r = vision.detect(f[:, :, ::-1])
                feed.publish_frame_result(r)
                time.sleep(args.interval_ms / 1000.0)
        finally:
            cap.release()
    else:
        frames = load_frames(args)
        print(f"[vision] {len(frames)} 帧待识别 "
              f"(frac_min={args.frac_min}, interval={args.interval_ms}ms)",
              flush=True)
        feed.run_frames(frames)
    feed.stop()
    print(f"[vision] 完成, 共发布 {feed.published} 个事件", flush=True)


if __name__ == "__main__":
    main()
