"""扫掠录像: 发 divert 同时抓帧, 事后帧差找推杆实际位置 + 香蕉轨迹."""
import time, json
import urllib.request, cv2, numpy as np
from ultralytics import YOLO

TARGET = int(__import__('sys').argv[1])
MODEL = '/tmp/banana_train/banana_v1-2/weights/best.pt'
STREAM = 'http://127.0.0.1:8125/stream'


def frames_for(seconds):
    resp = urllib.request.urlopen(urllib.request.Request(STREAM), timeout=15)
    buf = b''
    out = []
    end = time.time() + seconds
    while time.time() < end:
        chunk = resp.read(65536)
        if chunk:
            buf += chunk
        while True:
            a = buf.find(b'\xff\xd8')
            if a < 0:
                buf = buf[-8:]
                break
            b = buf.find(b'\xff\xd9', a + 2)
            if b < 0:
                if len(buf) > 4_000_000:
                    buf = buf[-8:]
                break
            f = cv2.imdecode(np.frombuffer(buf[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
            buf = buf[b + 2:]
            if f is not None:
                out.append(f)
    return out


def yolo_boxes(frames, yolo):
    out = []
    for f in frames:
        r = yolo(f, conf=0.25, iou=0.5, device='cpu', verbose=False)[0]
        if r.boxes is not None and len(r.boxes):
            x1, y1, x2, y2 = [int(v) for v in r.boxes.xyxy[0]]
            out.append((x1, y1, x2, y2))
        else:
            out.append(None)
    return out


def main():
    pre = frames_for(1.0)
    base = pre[-1]
    import paho.mqtt.client as mqtt
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'film-d{TARGET}')
    c.connect('127.0.0.1', 1883)
    c.subscribe(f'sort/sorter{TARGET}/telemetry')
    c.loop_start()
    sweep_start = time.time()
    cmd = json.dumps({'job_id': f'film-{TARGET}', 'action': 'divert',
                      'material': 'banana', 'qty': 1, 'trigger': 'immediate'})
    c.publish(f'sort/sorter{TARGET}/cmd', cmd, qos=1).wait_for_publish(2)
    print(f'[film] {time.strftime("%H:%M:%S")} 已发 {TARGET} 号仓 divert', flush=True)
    post = frames_for(6.0)
    c.loop_stop()
    c.disconnect()
    yolo = YOLO(MODEL)
    boxes = yolo_boxes([base] + post, yolo)
    print('[film] 香蕉轨迹 (相对扫掠命令 t=s):')
    for i, b in enumerate(boxes):
        t = (time.time() - sweep_start - 6.0) + i * 0.099  # 近似时刻
        if b:
            print(f'  帧{i:2d}: ({(b[0]+b[2])/2:.0f},{(b[1]+b[3])/2:.0f})')
        else:
            print(f'  帧{i:2d}: -')
    # 帧差找推杆: 扫掠中段帧 vs 扫掠前基准
    mids = post[10:40:3]   # 扫掠进行中的若干帧
    for i, f in enumerate(mids):
        d = cv2.absdiff(f, base).max(axis=2)
        mask = (d > 30).astype(np.uint8)
        ys, xs = np.nonzero(mask)
        if len(xs) < 200:
            print(f'[film] 中段帧{i}: 变化 {len(xs)}px (推杆不可见?)')
            continue
        # 香蕉区剔除 (用该帧的框)
        bb = boxes[len(pre[-1:] + post[:i*3 + 10]) - 1] if False else None
        cx, cy = xs.mean(), ys.mean()
        print(f'[film] 中段帧{i}: 变化区 质心=({cx:.0f},{cy:.0f}) {len(xs)}px '
              f'x∈[{xs.min()},{xs.max()}] y∈[{ys.min()},{ys.max()}]')
    cv2.imwrite(f'/tmp/film_base_s{TARGET}.jpg', base)
    cv2.imwrite(f'/tmp/film_mid_s{TARGET}.jpg', mids[len(mids)//2] if mids else base)
    print('[film] 完成: /tmp/film_base_s%d.jpg + film_mid_s%d.jpg' % (TARGET, TARGET))


if __name__ == '__main__':
    main()
