#!/usr/bin/env python3
"""执行前视觉检查: 取 MJPEG 新帧 -> Kimi VL (K2.8) 描述现场.
用法: cam_vision.py [额外指令]
每次 beltctl 执行前跑一遍; 输出简洁的中文场景描述.
"""
import sys, time, base64, os, json, subprocess, urllib.request

sys.path.insert(0, "/home/rockets/laya-industrial-bench")
sys.argv = ["beltctl.py", "1"]
import beltctl as B
import cv2

QWEN_URL = "http://192.168.8.56:8080/v1/chat/completions"
QWEN_MODEL = "Qwen3.8-27B"

def _qwen_key():
    k = os.environ.get("HERMES_CUSTOM_DFROBOT_AIGC_API_KEY")
    if k:
        return k
    for line in open("/home/rockets/.hermes/.env"):
        if line.startswith("HERMES_CUSTOM_DFROBOT_AIGC_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("无 DfRobot AIGC API key")

DEFAULT_PROMPT = """这是输送带分拣线的俯视画面。带面从右上到左下斜穿画面，物料流向是 右上->左下。
带面旁有三个推杆仓（按物料流向顺序 1 号、2 号、3 号），滑槽在带面两侧。
请简短回答:
1. 带面上有没有香蕉？有的话说位置（靠近画面哪个角/哪个推杆附近）和横向是否居中（偏左上边缘/居中/偏右下边缘）。
2. 各仓滑槽里有没有香蕉（大致数量即可，哪个仓有）。
3. 画面有无异常（模糊/发黑/被遮挡/卡帧迹象）。
要求：分点，总共不超过 120 字。"""

def stream_restart_local():
    print("[vision] 重启流服务...", flush=True)
    subprocess.run("pkill -f mjpeg_server.py", shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    subprocess.Popen([".venv/bin/python", "/tmp/mjpeg_server.py"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True,
                     cwd="/home/rockets/laya-industrial-bench")
    time.sleep(6)
    B.PULLER.reset()

def fresh_frame(max_wait=45):
    t_end = time.time() + max_wait
    tried_restart = False
    while time.time() < t_end:
        f = B.one_frame()
        if f is not None:
            return f
        if not tried_restart and time.time() > t_end - 25:
            stream_restart_local()
            tried_restart = True
        time.sleep(0.5)
    return None

def main():
    extra = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    prompt = DEFAULT_PROMPT + (("\n另外注意: " + extra) if extra else "")
    t0 = time.time()
    f = fresh_frame()
    if f is None:
        print(f"[vision] 失败: 45s 内无新鲜帧 (C110 卡死, 需拔插 USB 相机)")
        sys.exit(2)
    cv2.imwrite("/tmp/cam_vision_latest.jpg", f)
    b64 = base64.b64encode(open("/tmp/cam_vision_latest.jpg", "rb").read()).decode()
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]}],
    }
    req = urllib.request.Request(
        QWEN_URL, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + _qwen_key(), "Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=90))
    except Exception as e:
        print(f"[vision] Qwen 视觉调用失败: {repr(e)[:200]}")
        sys.exit(3)
    msg = resp["choices"][0]["message"]
    out = msg.get("content", "")
    print(f"[vision] 帧耗时 {time.time()-t0:.1f}s, 模型 {QWEN_MODEL}:")
    print(out)

if __name__ == "__main__":
    main()
