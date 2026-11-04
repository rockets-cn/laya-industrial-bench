"""推杆画面位置标定: 停带发 divert N, 录流帧差找推杆扫过带面的区域.
用法: python film_plate.py <bin:1|2|3>
输出: 推杆扫描区质心 + 与带心线的交点 (目标点候选).
带面有香蕉时自动从差集里扣除香蕉前后包围盒.
"""
import time, json, re, sys
import urllib.request, cv2, numpy as np
import paho.mqtt.client as mqtt
from ultralytics import YOLO

BIN = int(sys.argv[1]) if len(sys.argv) > 1 else 3
STREAM = 'http://127.0.0.1:8125/stream'
MODEL = '/tmp/banana_train/banana_v1-2/weights/best.pt'
Y = YOLO(MODEL)

# 带心线 (由香蕉轨迹观测拟合): 进口 -> 3号杆 -> 出画
CENTERLINE = np.array([(566, 118), (480, 209), (402, 240), (346, 340),
                       (300, 352), (246, 334), (170, 430)], np.float32)
BAND_W = 160   # 带面半宽 (px)


def get_frame():
    """取一帧 (新连接, 最新帧)."""
    resp = urllib.request.urlopen(urllib.request.Request(STREAM), timeout=10)
    buf = b""
    end = time.time() + 1.5
    while time.time() < end:
        chunk = resp.read(65536)
        if chunk:
            buf += chunk
        a = buf.find(b"\xff\xd8")
        if a < 0:
            continue
        b = buf.find(b"\xff\xd9", a + 2)
        if b >= 0:
            f = cv2.imdecode(np.frombuffer(buf[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
            if f is not None:
                return f
    return None


def banana_box(f, pad=25):
    r = Y(f, conf=0.25, iou=0.5, device='cpu', verbose=False)[0]
    if r.boxes is None or len(r.boxes) == 0:
        return None
    x1, y1, x2, y2 = [int(v) for v in r.boxes.xyxy[0]]
    h, w = f.shape[:2]
    return (max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad))


def dist_to_polyline(pt, poly):
    best = 1e9
    for i in range(len(poly) - 1):
        a, b = poly[i], poly[i + 1]
        ab = b - a
        t = np.clip(np.dot(pt - a, ab) / (np.dot(ab, ab) + 1e-9), 0, 1)
        d = np.linalg.norm(pt - (a + t * ab))
        best = min(best, d)
    return best


def main():
    MQ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"filmplate{BIN}")
    MQ.connect("127.0.0.1", 1883)
    MQ.loop_start()
    MQ.publish("sort/line1/conveyor/cmd",
               json.dumps({"job_id": f"filmplate{BIN}-stop", "action": "stop"}),
               qos=1).wait_for_publish(2)
    time.sleep(1.5)

    base = get_frame()
    bb0 = banana_box(base)
    print(f"基线帧就绪, 香蕉框: {bb0}", flush=True)

    frames = []   # (ts_s, frame)
    divert_at = time.time() + 2.5
    t_end = time.time() + 9.0
    resp = urllib.request.urlopen(urllib.request.Request(STREAM), timeout=30)
    buf = b""
    fired = False
    seen = set()
    while time.time() < t_end:
        if not fired and time.time() >= divert_at:
            MQ.publish(f"sort/sorter{BIN}/cmd", json.dumps(
                {"job_id": f"filmplate{BIN}", "action": "divert",
                 "material": "banana", "qty": 1, "trigger": "immediate"}),
                qos=1).wait_for_publish(2)
            fired = True
            print("divert 已发", flush=True)
        chunk = resp.read(65536)
        if not chunk:
            continue
        buf += chunk
        while True:
            a = buf.find(b"\xff\xd8")
            if a < 0:
                buf = buf[-8:]
                break
            b = buf.find(b"\xff\xd9", a + 2)
            if b < 0:
                if len(buf) > 4_000_000:
                    buf = buf[-8:]
                break
            m = re.findall(rb"X-Timestamp: (\d+)", buf[:a])
            ts = int(m[-1]) if m else int(time.time() * 1000)
            f = cv2.imdecode(np.frombuffer(buf[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
            buf = buf[b + 2:]
            if f is not None and ts not in seen:
                seen.add(ts)
                frames.append((time.time(), f))
    time.sleep(1.0)
    after = get_frame()
    bb1 = banana_box(after)
    print(f"扫掠后帧, 香蕉框: {bb1}", flush=True)

    # 帧差: 相对基线
    diffs = []
    for t, f in frames:
        d = cv2.absdiff(base, f).max(axis=2)
        mask = d > 30
        diffs.append((t, mask, f))

    # 排除香蕉前后区域
    excl = np.zeros(base.shape[:2], bool)
    for bb in (bb0, bb1):
        if bb:
            excl[bb[1]:bb[3], bb[0]:bb[2]] = True

    # 诊断: 相邻帧差 (判断流是否稳定)
    if len(frames) > 2:
        adj = cv2.absdiff(frames[10][1], frames[20][1]).max(axis=2)
        print(f"诊断: 相邻帧(隔10帧)差>30 px = {(adj > 30).sum()} (应很小)", flush=True)
    # 找相对基线差最大的帧可视化
    areas0 = [(i, int((diffs[i][1] & ~excl).sum())) for i in range(len(diffs))]
    i_max = max(areas0, key=lambda x: x[1])[0]
    dvis = cv2.absdiff(base, diffs[i_max][2]).max(axis=2)
    cv2.imwrite('/tmp/filmplate_diff_max.jpg', np.clip(dvis * 3, 0, 255).astype(np.uint8))
    print(f"诊断: 最大差帧 #{i_max} 面积 {areas0[i_max][1]}px, 已存 /tmp/filmplate_diff_max.jpg", flush=True)

    areas = [(i, int((diffs[i][1] & ~excl).sum())) for i in range(len(diffs))]
    active = [i for i, a in areas if a > 600]
    print(f"帧 {len(diffs)} 帧, 扫掠活跃帧 (差>600px, 扣除香蕉): {len(active)}", flush=True)
    if not active:
        print("未检测到扫掠区域, 失败")
        return

    # 扫掠区域 = 活跃帧差集的并 (扣除香蕉)
    region = np.zeros(base.shape[:2], bool)
    for i in active:
        region |= diffs[i][1] & ~excl
    region = cv2.dilate(region.astype(np.uint8) * 255, np.ones((5, 5), np.uint8), iterations=2) > 0

    # 与带面带相交: 带心线膨胀 BAND_W
    band = np.zeros(base.shape[:2], bool)
    for i in range(len(CENTERLINE) - 1):
        cv2.line(band, tuple(CENTERLINE[i].astype(int)), tuple(CENTERLINE[i + 1].astype(int)),
                 True, thickness=BAND_W)
    inter = region & band
    if inter.sum() < 100:
        # 交区太小: 用最近点法
        ys, xs = np.where(region)
        best_pt, best_d = None, 1e9
        for x, y in zip(xs[::7], ys[::7]):
            d = dist_to_polyline(np.array([x, y], np.float32), CENTERLINE)
            if d < best_d:
                best_d, best_pt = d, (x, y)
        print(f"交区过小, 最近点: {best_pt} (距带心 {best_d:.0f}px)")
        target = np.array(best_pt, np.float32)
    else:
        ys, xs = np.where(inter)
        target = np.array([xs.mean(), ys.mean()], np.float32)
        print(f"扫掠区∩带面: {inter.sum()} px, 质心 {tuple(np.round(target).astype(int))}, "
              f"x范围 [{xs.min()},{xs.max()}] y范围 [{ys.min()},{ys.max()}]", flush=True)

    # 目标点: 交区中沿带心线最靠下游的点 (推料时香蕉中心应在推杆内侧)
    print(f">>> {BIN} 号仓推杆目标点 (画面): {tuple(np.round(target).astype(int))} "
          f"(距带心线 {dist_to_polyline(target, CENTERLINE):.0f}px)", flush=True)
    cv2.imwrite(f'/tmp/filmplate{BIN}_region.jpg',
                cv2.bitwise_and(base, base, mask=region.astype(np.uint8) * 255))
    cv2.imwrite(f'/tmp/filmplate{BIN}_base.jpg', base)


if __name__ == "__main__":
    main()
