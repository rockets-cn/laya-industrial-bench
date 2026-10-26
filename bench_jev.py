"""Benchmark the local Jev-compatible API: latency + accuracy over repeated runs.

Sends a fixed labeled set (English + Chinese, all three question types) to the
running server REPS times, single-threaded, then a short 4-way concurrent burst.
Reports latency percentiles, per-type accuracy, and answer determinism.

Usage: .venv/bin/python bench_jev.py [reps]   (default 20)
"""

import json
import statistics
import sys
import time
import concurrent.futures as cf
import urllib.request

URL = "http://127.0.0.1:8000/v1/systemone"
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 20

DEPT = {"type": "choice", "instructions": "Which department should handle this?",
        "criteria": {"billing": "invoices, payments, refunds",
                      "technical": "bugs, outages, system errors",
                      "other": "everything else"}}
URG3 = {"type": "score", "instructions": "How urgent is this?",
        "criteria": ["not urgent", "needs attention soon", "blocking"]}
ANGRY = {"type": "noul", "instructions": "Is the user angry or upset?"}

# (state, questions, {qid: expected}); expected: choice -> option, noul -> yes/no, score -> level
CASES = [
    # --- English ---
    ("Hi, we were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
     {"department": DEPT,
      "churn": {"type": "noul", "instructions": "Does the user threaten to cancel or leave?"}},
     {"department": "billing", "churn": "yes"}),

    ("The app crashes every time I open the settings screen on my phone.",
     {"department": DEPT},
     {"department": "technical"}),

    ("I would like to upgrade to the enterprise plan. What are the pricing tiers?",
     {"department": {"type": "choice", "instructions": "Which department should handle this?",
                      "criteria": {"billing": "invoices, payments, refunds",
                                    "technical": "bugs, outages",
                                    "sales": "pricing, new contracts"}},
      "urgent": {"type": "noul", "instructions": "Is this request time-critical or blocking?"}},
     {"department": "sales", "urgent": "no"}),

    ("Help! All our payments are failing right now and the site is down for every customer.",
     {"urgent": URG3, "angry": ANGRY},
     {"urgent": 2, "angry": "yes"}),

    ("Just a small question about the invoice format, no rush at all.",
     {"urgent": URG3, "angry": ANGRY},
     {"urgent": 0, "angry": "no"}),

    # --- Chinese ---
    ("3月份被重复扣了两次款，请尽快把多收的钱退给我。",
     {"department": DEPT,
      "refund": {"type": "noul", "instructions": "Does the user explicitly request a refund?"}},
     {"department": "billing", "refund": "yes"}),

    ("手机打开设置页面时应用总是闪退，已经好几天了。",
     {"department": DEPT},
     {"department": "technical"}),

    ("我这边所有支付都失败了，网站对全部客户都打不开，现在就需要人处理！",
     {"urgent": URG3, "angry": ANGRY},
     {"urgent": 2, "angry": "yes"}),

    ("想升级成企业版，请问价格方案是怎么样的？",
     {"department": {"type": "choice", "instructions": "Which department should handle this?",
                      "criteria": {"billing": "payments, refunds",
                                    "technical": "bugs",
                                    "sales": "pricing, new contracts"}},
      "urgent": {"type": "noul", "instructions": "Is this request time-critical?"}},
     {"department": "sales", "urgent": "no"}),

    ("想问一下发票的格式问题，不着急，方便的时候回复就行。",
     {"urgent": URG3, "angry": ANGRY},
     {"urgent": 0, "angry": "no"}),
]


def call(state, questions, model="jev-latest"):
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return time.perf_counter() - t0, data


def check(ans, expected):
    t = ans["type"]
    if t == "choice":
        return ans["choice"] == expected
    if t == "noul":
        return ("yes" if ans["noul"] >= 0.5 else "no") == expected
    # score: expected level; strict - the weighted score must round to that level
    return round(ans["score"]) == expected


def main():
    print(f"cases={len(CASES)} reps={REPS} endpoint={URL}")

    dt, _ = call(*CASES[0][:2])
    print(f"warmup: {dt*1000:.0f} ms")

    lat = []
    correct = total = 0
    per_type = {}
    first_pass = {}
    deterministic = True
    wrong = []

    for rep in range(REPS):
        for case_idx, (state, questions, expected) in enumerate(CASES):
            dt, data = call(state, questions)
            lat.append(dt)
            for qid, exp in expected.items():
                total += 1
                a = data["answers"][qid]
                ok = check(a, exp)
                correct += ok
                d = per_type.setdefault(a["type"], [0, 0])
                d[0] += ok
                d[1] += 1
                if not ok:
                    wrong.append((case_idx, qid, exp, a))
                key = (case_idx, qid)
                snap = json.dumps(a, sort_keys=True)
                if key not in first_pass:
                    first_pass[key] = snap
                elif first_pass[key] != snap:
                    deterministic = False

    lat_ms = sorted(x * 1000 for x in lat)
    n = len(lat_ms)
    q = lambda p: lat_ms[min(n - 1, int(p * n))]
    print("\n=== Latency (single-threaded, ms) ===")
    print(f"calls: {n}  mean: {statistics.mean(lat_ms):.0f}  median: {q(.5):.0f}  "
          f"p90: {q(.9):.0f}  p95: {q(.95):.0f}  max: {lat_ms[-1]:.0f}  min: {lat_ms[0]:.0f}")
    print(f"throughput: {n / (sum(lat_ms)/1000):.1f} calls/s")

    print("\n=== Accuracy ===")
    print(f"overall: {correct}/{total} = {100*correct/total:.1f}%")
    for t, (ok, tot) in sorted(per_type.items()):
        print(f"  {t:8s} {ok}/{tot} = {100*ok/tot:.1f}%")
    if wrong:
        print("misses (unique):")
        seen = set()
        for ci, qid, exp, a in wrong:
            k = (ci, qid)
            if k in seen:
                continue
            seen.add(k)
            print(f"  case {ci} {qid}: expected {exp!r} got {a}")

    print(f"\n=== Determinism over {REPS} repeated passes ===")
    print("identical answers every run:", "YES" if deterministic else "NO")

    print("\n=== Concurrency (4 workers, 40 calls) ===")
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda i: call(*CASES[i % len(CASES)][:2]), range(40)))
    wall = time.perf_counter() - t0
    bl = sorted(x * 1000 for x, _ in results)
    print(f"wall: {wall:.1f}s  throughput: {40/wall:.1f} calls/s  "
          f"median: {bl[len(bl)//2]:.0f} ms  p95: {bl[int(.95*len(bl))]:.0f} ms  max: {bl[-1]:.0f} ms")


if __name__ == "__main__":
    main()
