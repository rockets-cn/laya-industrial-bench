"""恒速带测试: 盯香蕉 x 坐标, 到目标杆位阈值就发 divert (每目标只发一次).

用法: python monitor_divert.py <target:1|3> <threshold_x>
  target=1: 1号杆(进口端, 画面x≈434), 建议阈值 480
  target=3: 3号杆/白杆(出口, 画面x≈246), 建议阈值 300
带速 0.075 m/s (画面中部 ~2px/mm).
"""
import sys, time, json
import urllib.request, cv2, numpy as np
from ultralytics import YOLO

TARGET = int(sys.argv[1])
THRESHOLD = float(sys.argv[2])
MODEL = '/tmp/banana_train/banana_v1-2/weights/best.pt'
STREAM = 'http://127.0.0.1:8125/stream'


def one_frame():
    """取 1 帧 (不缓存整段流, 快)."""
    resp = urllib.request.urlopen(urllib.request.Request(STREAM), timeout=10)
    buf = b''
    end = time.time() + 0.9
    while time.time() < end:
        chunk = resp.read(65536)
        if chunk:
            buf += chunk
        a = buf.find(b'\xff\xd8')
        if a < 0:
            continue
        b = buf.find(b'\xff\xd9', a + 2)
        if b >= 0:
            f = cv2.imdecode(np.frombuffer(buf[a:b + 2], np.uint8), cv2.IMREAD_COLOR)
            if f is not None:
                return f
    return None


def main():
    target, threshold = TARGET, THRESHOLD
    yolo = YOLO(MODEL)
    print(f'[mon] 目标={target} 号仓 阈值x<{threshold} 带速0.075m/s', flush=True)
    time.sleep(1.0)   # 等稳定
    fired = False
    last_log = 0.0
    while True:
        f = one_frame()
        if f is None:
            time.sleep(0.3)
            continue
        r = yolo(f, conf=0.25, iou=0.5, device='cpu', verbose=False)[0]
        if r.boxes is None or len(r.boxes) == 0:
            if time.time() - last_log > 3:
                last_log = time.time()
                print('[mon] 画面无香蕉', flush=True)
            time.sleep(0.25)
            continue
        x1, y1, x2, y2 = [int(v) for v in r.boxes.xyxy[0]]
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if time.time() - last_log > 0.8:
            last_log = time.time()
            print(f'[mon] 香蕉 cx={cx:.0f} cy={cy:.0f} (目标<{threshold})', flush=True)
        if not fired and cx < threshold:
            fired = True
            import paho.mqtt.client as mqtt
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'mon-d{target}')
            c.connect('127.0.0.1', 1883)
            cmd = json.dumps({'job_id': f'mon-{target}-belt075', 'action': 'divert',
                              'material': 'banana', 'qty': 1, 'trigger': 'immediate'})
            c.publish(f'sort/sorter{target}/cmd', cmd, qos=1).wait_for_publish(2)
            c.disconnect()
            print(f'[mon] >>> cx={cx:.0f} < {threshold} 已发 {target} 号仓 divert <<<',
                  flush=True)
            time.sleep(8)   # 等扫掠完
            cv2.imwrite(f'/tmp/mon_after_s{target}.jpg', f)
            print('[mon] 扫掠窗口结束', flush=True)
            break
        time.sleep(0.15)


if __name__ == '__main__':
    main()
