"""Fruit-classification benchmark for the local Laya Jev API.

Task: classify a text description into one of four fruits:
红苹果 (red apple), 绿苹果 (green apple), 黄香蕉 (yellow banana), 梨 (pear).

Measures latency percentiles + per-class accuracy over repeated passes.

Usage: .venv/bin/python bench_fruit.py [reps]   (default 10)
"""

import json
import statistics
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8000/v1/systemone"
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

import os

_USE_RICH = os.environ.get("RICH", "1") == "1"

CRITERIA_BASE = {
    "red_apple":   "红苹果: 红色, 圆形, 表皮光滑, 脆甜 (red, round, shiny, crisp, sweet)",
    "green_apple": "绿苹果: 绿色, 圆形, 脆, 略酸 (green, round, crisp, slightly tart)",
    "banana":      "黄香蕉: 黄色, 长条形, 弯曲, 剥皮吃 (yellow, long, curved, peeled)",
    "pear":        "梨: 黄色或黄绿色, 上窄下宽, 多汁 (yellow/greenish, narrow top, juicy)",
}

# 加强版: 每类给出完整的判别特征清单 + 易混淆点提示, 要求按特征逐项匹配
CRITERIA_RICH = {
    "red_apple": "红苹果 (red apple). 特征: 颜色=红色; 形状=圆形/球形; 手感=光滑硬实; "
                 "口感=脆、甜、多汁; 顶部有短茎, 切开中间有籽. "
                 "只有当描述同时符合 红色+圆形 时才选这个. 长条形、弯曲、上窄下宽的水果不是它.",
    "green_apple": "绿苹果 (green apple). 特征: 颜色=绿色/青绿色; 形状=圆形/球形; "
                  "口感=脆、偏酸. 必须是 圆形 的. 长条形或上窄下宽的绿色水果不是它.",
    "banana": "香蕉 (banana). 特征: 颜色=黄色(成熟时); 形状=长条形、细长、弯曲、弧形; "
              "吃法=剥皮吃, 果皮剥下来连着, 果肉白色软糯; 常成串挂在架上; 一根一根的. "
              "关键: 长条+弯曲+剥皮 这三项出现即选香蕉, 即使没出现'香蕉'二字. "
              "圆形的红/绿水果不是它.",
    "pear": "梨 (pear). 特征: 颜色=黄色/黄绿色/浅绿色; 形状=上窄下宽, 一头小一头大, 梨形; "
           "口感=多汁, 汁水多; 可做梨汁; 削皮吃. 关键: 上窄下宽/一头小一头大/梨形 是梨的标志, "
           "出现即选梨, 即使没出现'梨'字. 圆形或长条形水果不是它.",
}

FRUIT = {"type": "choice",
         "instructions": "这是哪种水果?Which fruit is described here?",
         "criteria": (CRITERIA_RICH if _USE_RICH else CRITERIA_BASE)}

# (state, expected)
CASES = [
    # --- 中文描述 ---
    ("桌上放着一个红色的水果, 圆圆的, 表皮很光滑, 咬起来又脆又甜。", "red_apple"),
    ("我咬了一口绿色的水果, 圆圆的, 很脆, 味道有点酸。", "green_apple"),
    ("她剥开一根黄色的长条水果, 弯弯曲曲的, 果肉软软的。", "banana"),
    ("这个水果上头尖尖的, 下面圆圆的, 黄绿色, 汁水很多。", "pear"),
    ("早上吃了一个红红的苹果, 脆生生的。", "red_apple"),
    ("青苹果, 绿的, 酸酸甜甜的。", "green_apple"),
    ("一串黄色的水果挂在架上, 弯弯的, 皮剥下来连着。", "banana"),
    ("削了个梨, 黄黄的, 汁水顺着刀流下来。", "pear"),
    ("他手里拿着一个又圆又红的水果, 上面还有个短茎。", "red_apple"),
    ("这个水果是绿色的, 圆得像球, 咬一口嘎嘣脆, 还有点酸。", "green_apple"),
    ("早餐买了一根黄色的, 弯弯的, 剥开皮里面是白的。", "banana"),
    ("她喝了一杯梨汁, 那个水果黄绿色, 形状一头小一头大。", "pear"),
    ("红色的, 圆的, 闻着有果香, 切开会看到中间的籽。", "red_apple"),
    # --- 英文描述 ---
    ("A shiny red round fruit, crisp and sweet when bitten.", "red_apple"),
    ("I bit into a green round fruit; it was crisp and a bit sour.", "green_apple"),
    ("He peeled a long curved yellow fruit; the flesh was soft inside.", "banana"),
    ("The fruit is yellow-green, wider at the bottom than the top, and very juicy.", "pear"),
    ("A round red fruit with a short stem on top, smells like orchard fruit.", "red_apple"),
    ("A ball-sized green fruit, crunchy and tart.", "green_apple"),
    ("A yellow banana-shaped fruit, peeled in one long strip.", "banana"),
    ("A pear-shaped fruit, yellowish, dripping with juice.", "pear"),
]


def call(state):
    body = json.dumps({"state": state, "model": "jev-latest", "questions": {"fruit": FRUIT}}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return time.perf_counter() - t0, data


def main():
    print(f"criteria={'RICH' if _USE_RICH else 'BASE'}  cases={len(CASES)} reps={REPS} endpoint={URL}")
    dt, _ = call(CASES[0][0])
    print(f"warmup: {dt*1000:.0f} ms")

    lat, per_class, per_case_ok, wrong = [], {}, {}, []
    for rep in range(REPS):
        for ci, (state, exp) in enumerate(CASES):
            dt, data = call(state)
            lat.append(dt)
            a = data["answers"]["fruit"]
            ok = a["choice"] == exp
            per_case_ok[(ci, exp)] = per_case_ok.get((ci, exp), 0) + ok
            d = per_class.setdefault(exp, [0, 0])
            d[1] += 1
            d[0] += ok
            if not ok:
                wrong.append((ci, exp, a["choice"], a.get("probabilities", {}), state))

    lat_ms = sorted(x * 1000 for x in lat)
    n = len(lat_ms)
    q = lambda p: lat_ms[min(n - 1, int(p * n))]
    print("\n=== 效率 Latency (single-threaded, ms) ===")
    print(f"calls: {n}  mean: {statistics.mean(lat_ms):.0f}  median: {q(.5):.0f}  "
          f"p90: {q(.9):.0f}  p95: {q(.95):.0f}  max: {lat_ms[-1]:.0f}  min: {lat_ms[0]:.0f}")
    print(f"throughput: {n / (sum(lat_ms)/1000):.1f} calls/s")

    total_t = sum(t for _, t in per_class.values())
    total_c = sum(c for c, _ in per_class.values())
    print("\n=== 准确度 Accuracy ===")
    print(f"overall: {total_c}/{total_t} = {100*total_c/total_t:.1f}%")
    for cls in ("red_apple", "green_apple", "banana", "pear"):
        c, t = per_class.get(cls, (0, 0))
        print(f"  {cls:12s} {c}/{t} = {100*c/t:.0f}%")

    print("\n=== 逐条正确率 (每 rep) ===")
    for ci, (state, exp) in enumerate(CASES):
        okc = per_case_ok.get((ci, exp), 0)
        mark = "✓" if okc == REPS else ("✗" if okc == 0 else "±")
        print(f"  {mark} case {ci:2d} expect {exp:12s} {okc}/{REPS}  {state[:40]}")

    if wrong:
        print("\n=== 错例 (unique) ===")
        seen = set()
        for ci, exp, got, probs, state in wrong:
            if ci in seen:
                continue
            seen.add(ci)
            pp = ", ".join(f"{k}={v:.2f}" for k, v in sorted(probs.items(), key=lambda x: -x[1]))
            print(f"  case {ci}: expect {exp} got {got}  [{pp}]")
            print(f"      {state}")


if __name__ == "__main__":
    main()
