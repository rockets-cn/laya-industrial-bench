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


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default="tcp://127.0.0.1:1883")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--gen", action="store_true",
                     help="合成测试图 (默认; 离线可跑)")
    src.add_argument("--file", default=None, help="单张图片")
    src.add_argument("--camera", type=int, default=None, help="摄像头索引")
    ap.add_argument("--interval-ms", type=int, default=250,
                    help="帧间隔 (ms)")
    ap.add_argument("--frac-min", type=float, default=0.08)
    ap.add_argument("--gen-single", action="store_true",
                    help="--gen 只发前两张单色图 (模拟单物料)")
    a = ap.parse_args()
    if not (a.file or a.camera is not None):
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
