"""颜色方块识别基准 (像素层 HSV 阈值 + 可选 Laya 交叉校验).

关键结论先说: Laya 是纯文本决策模型, 看不到像素. "识别不同的颜色块"
必须由像素层完成 (HSV 阈值, ms 级), Laya 只在下游做决策/与 ASR 文本交叉校验.

用法:
    .venv/bin/python bench_colorblock.py [reps]          # 生成测试图, 默认 20 reps
    .venv/bin/python bench_colorblock.py 5 --camera 0    # 从 0 号摄像头读 5 帧
    .venv/bin/python bench_colorblock.py 5 --file a.png  # 识别一张图片
"""

import json
import statistics
import sys
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw

LAYA = "http://127.0.0.1:8000/v1/systemone"

# HSV (H: 0-360, S/V: 0-1). 红色跨 0/360 边界, 用双区间.
COLORS = {
    "red":    {"cn": "红色", "h": [(0, 15), (345, 360)], "s_min": 0.35, "v_min": 0.30},
    "yellow": {"cn": "黄色", "h": [(35, 65)],            "s_min": 0.40, "v_min": 0.40},
    "green":  {"cn": "绿色", "h": [(75, 165)],           "s_min": 0.30, "v_min": 0.25},
    "blue":   {"cn": "蓝色", "h": [(195, 255)],          "s_min": 0.30, "v_min": 0.25},
    "white":  {"cn": "白色", "h": None,                  "s_max": 0.18, "v_min": 0.85},
    "black":  {"cn": "黑色", "h": None,                  "v_max": 0.18},
}
BG = (110, 112, 114)  # 中性灰背景 (低饱和, 不会被误判)


def rgb_to_hsv(rgb: np.ndarray):
    """uint8 RGB (H,W,3) -> (H, S, V), H in [0,360), S/V in [0,1]. 纯 numpy."""
    r, g, b = rgb[..., 0] / 255.0, rgb[..., 1] / 255.0, rgb[..., 2] / 255.0
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 0
    m1 = (mx == r) & m
    h[m1] = ((g[m1] - b[m1]) / d[m1]) % 6.0
    m2 = (mx == g) & m
    h[m2] = ((b[m2] - r[m2]) / d[m2] + 2.0)
    m3 = (mx == b) & m
    h[m3] = ((r[m3] - g[m3]) / d[m3] + 4.0)
    h *= 60.0
    s = np.where(mx == 0, 0.0, d / mx)
    return h, s, mx


def detect(frame: np.ndarray, scale: int = 320, frac_min: float = 0.04):
    """frame (H,W,3) uint8 -> (dt_s, [ {key,cn,frac} ... ]). 大边缩到 scale 再算.
    frac_min: 某色像素占比低于此值视为噪声, 不报."""
    t0 = time.perf_counter()
    if max(frame.shape[:2]) > scale:
        k = scale / max(frame.shape[:2])
        frame = np.asarray(Image.fromarray(frame).resize(
            (max(1, round(frame.shape[1] * k)), max(1, round(frame.shape[0] * k)))))
    h, s, v = rgb_to_hsv(frame)
    n = h.size
    found = []
    for key, c in COLORS.items():
        if "h" in c and c["h"]:
            mask = np.zeros_like(h, dtype=bool)
            for lo, hi in c["h"]:
                mask |= (h >= lo) & (h <= hi)
        else:
            mask = np.ones_like(h, dtype=bool)
        mask &= s >= c.get("s_min", 0.0)
        mask &= v >= c.get("v_min", 0.0)
        if "s_max" in c:
            mask &= s <= c["s_max"]
        if "v_max" in c:
            mask &= v <= c["v_max"]
        frac = mask.sum() / n
        if frac >= frac_min:
            found.append({"key": key, "cn": c["cn"], "frac": round(frac, 4)})
    found.sort(key=lambda x: -x["frac"])
    return time.perf_counter() - t0, found


def make_test_frames():
    """生成 4 张测试图: 单色 / 双色 / 四色 / 加噪四色. 每张一个 2x2 方块网格."""
    W, H, S = 640, 480, 220

    def put(colors, noise=0.0):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        for i, c in enumerate(colors):
            x0, y0 = (i % 2) * (S + 40), (i // 2) * (S + 40)
            d.rectangle([x0, y0, x0 + S, y0 + S], fill=c)
        arr = np.asarray(img, dtype=np.uint8)
        if noise > 0:
            arr = np.clip(arr.astype(np.int32)
                          + np.random.default_rng(42).normal(0, noise, arr.shape).astype(np.int32),
                          0, 255).astype(np.uint8)
        return arr

    return [
        ("单色-红",   put([(200, 30, 30)]),            {"red"}),
        ("双色-红蓝", put([(200, 30, 30), (30, 60, 210)]), {"red", "blue"}),
        ("四色",     put([(200, 30, 30), (30, 60, 210), (30, 190, 70), (230, 200, 30)]),
                      {"red", "blue", "green", "yellow"}),
        ("四色+噪声", put([(200, 30, 30), (30, 60, 210), (30, 190, 70), (230, 200, 30)],
                         noise=12), {"red", "blue", "green", "yellow"}),
    ]


def laya_crosscheck(found):
    """像素层报告的主色 -> Laya 确认 (演示 Laya 在回路里的位置: 决策/校验, 非识别).
    返回 answer dict; 服务不可用返回 None."""
    if not found:
        return None
    top = found[0]
    state = "摄像头识别报告: 画面里有一个" + top["cn"] + "方块"
    questions = {"color": {"type": "choice",
                           "instructions": "画面里的方块是什么颜色?",
                           "criteria": {k: c["cn"] for k, c in COLORS.items()
                                         if k in ("red", "yellow", "green", "blue")}}}
    body = json.dumps({"state": state, "model": "jev-latest",
                       "questions": questions}).encode()
    req = urllib.request.Request(LAYA, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    return data["answers"]["color"]


def main():
    reps = int(sys.argv[1]) if len(sys.argv[1]) > 0 and sys.argv[1].isdigit() else 20
    src = "gen"
    frames = []
    if "--camera" in sys.argv:
        src = "camera"
        idx = int(sys.argv[sys.argv.index("--camera") + 1])
        import cv2
        cap = cv2.VideoCapture(idx)
        assert cap.isOpened(), f"打不开摄像头 {idx}"
        cap.read()  # 丢弃首帧 (自动曝光未稳)
        for _ in range(reps):
            ok, f = cap.read()
            if ok:
                frames.append(f[:, :, ::-1])  # BGR->RGB
        cap.release()
    elif "--file" in sys.argv:
        src = "file"
        frames = [np.asarray(Image.open(sys.argv[sys.argv.index("--file") + 1]).convert("RGB"))]
        reps = 1
    else:
        frames = [f for _, f, _ in make_test_frames()]

    print(f"src={src}  frames={len(frames)}  reps={reps}  "
          f"Laya endpoint={LAYA}\n")

    # 正确性: 每张图识别出的颜色集合
    print("=== 识别结果 (生成图) ===")
    if src == "gen":
        for name, f, expect in make_test_frames():
            _, found = detect(f)
            got = {x["key"] for x in found}
            ok = got == expect
            print(f"  {'OK ' if ok else 'FAIL'} {name}: 期望 {sorted(expect)}  实际 {sorted(got)}")

    # 时延
    lat = []
    for rep in range(reps):
        for f in frames:
            dt, _ = detect(f)
            lat.append(dt * 1000)
    lat.sort()
    n = len(lat)
    q = lambda p: lat[min(n - 1, int(p * n))]
    print("\n=== 像素层时延 (numpy HSV, 缩到 320px) ===")
    print(f"calls: {n}  mean: {statistics.mean(lat):.1f} ms  median: {q(.5):.1f}  "
          f"p95: {q(.95):.1f}  max: {lat[-1]:.1f}  min: {lat[0]:.1f}")

    # Laya 交叉校验 (可选)
    print("\n=== Laya 交叉校验 (像素报告 -> Laya 确认, 单问) ===")
    try:
        dt, found = detect(frames[0])
        t1 = time.perf_counter()
        ans = laya_crosscheck(found)
        dt_l = time.perf_counter() - t1
        if ans:
            print(f"  像素层 {dt*1000:.1f} ms -> Laya {dt_l*1000:.0f} ms, 选择 {ans['choice']} "
                  f"(conf {ans['confidence']:.2f}), E2E {dt*1000+dt_l*1000:.0f} ms")
            print("  注意: Laya 在这里只是确认/校验; 颜色识别本身发生在像素层 (~ms).")
        else:
            print("  Laya 服务不可用, 跳过")
    except Exception as e:
        print(f"  跳过: {e}")


if __name__ == "__main__":
    main()
