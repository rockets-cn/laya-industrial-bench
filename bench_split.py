"""A/B test: single multi-question request vs per-intent split requests.

Same state sentence, three binary intent questions (want apple/pear/banana).
Method A: one HTTP call with all three noul questions.
Method B: three HTTP calls, one question each (per-intent split).

Reports total latency per sentence and per-question accuracy.

Usage: .venv/bin/python bench_split.py [reps]   (default 10)
"""

import json
import statistics
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8000/v1/systemone"
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

# (state, expected want: apple, pear, banana)
CASES = [
    ("我要那个苹果，我要那个梨，给我十个香蕉", {"apple": True, "pear": True, "banana": True}),
    ("来两个红苹果和一把香蕉", {"apple": True, "pear": False, "banana": True}),
    ("只给我三个梨就行", {"apple": False, "pear": True, "banana": False}),
    ("一个苹果，不要梨，五个香蕉谢谢", {"apple": True, "pear": False, "banana": True}),
]

ASK = {
    "apple":  "用户是否想要苹果?",
    "pear":   "用户是否想要梨?",
    "banana": "用户是否想要香蕉?",
}


def call(questions, state):
    body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return time.perf_counter() - t0, data


def decide(noul):
    return "yes" if noul >= 0.5 else "no"


def main():
    print(f"cases={len(CASES)} reps={REPS} threshold=0.5")
    call({"apple": {"type": "noul", "instructions": ASK["apple"]}}, CASES[0][0])
    print("warmup done\n")

    for name, method in (("A: 单请求多问题 (1 call, 3 questions)", "single"),
                          ("B: 逐意图拆分 (3 calls, 1 question each)", "split")):
        lat, ok_all, per_fruit, wrong = [], 0, {}, []
        for rep in range(REPS):
            for ci, (state, exp) in enumerate(CASES):
                if method == "single":
                    qs = {f: {"type": "noul", "instructions": ASK[f]} for f in ASK}
                    dt, resp = call(qs, state)
                    data = resp["answers"]
                else:
                    t0 = time.perf_counter()
                    data = {}
                    for f in ASK:
                        _, d = call({f: {"type": "noul", "instructions": ASK[f]}}, state)
                        data[f] = d["answers"][f]
                    dt = time.perf_counter() - t0
                lat.append(dt)
                case_ok = True
                for f, want in exp.items():
                    a = data[f]
                    got = decide(a["noul"])
                    ok = (got == ("yes" if want else "no"))
                    d = per_fruit.setdefault(f, [0, 0])
                    d[1] += 1
                    d[0] += ok
                    case_ok &= ok
                    if not ok:
                        wrong.append((ci, f, want, a["noul"], state))
                ok_all += case_ok
        lat_ms = sorted(x * 1000 for x in lat)
        n = len(lat_ms)
        q = lambda p: lat_ms[min(n - 1, int(p * n))]
        total_q = sum(t for _, t in per_fruit.values())
        total_c = sum(c for c, _ in per_fruit.values())
        calls = REPS * len(CASES) * (1 if method == "single" else 3)
        print(f"===== {name} =====")
        print(f"wall latency per sentence: mean {statistics.mean(lat_ms):.0f} ms  "
              f"median {q(.5):.0f}  p95 {q(.95):.0f}  max {lat_ms[-1]:.0f}")
        print(f"HTTP calls used: {calls}  -> effective {n / (sum(lat_ms)/1000 * len(CASES)):.1f} sentences/s")
        print(f"sentence fully correct: {ok_all}/{REPS*len(CASES)} = {100*ok_all/(REPS*len(CASES)):.0f}%")
        print(f"question accuracy: {total_c}/{total_q} = {100*total_c/total_q:.1f}%")
        for f in ("apple", "pear", "banana"):
            c, t = per_fruit.get(f, (0, 0))
            print(f"  {f:7s} {c}/{t} = {100*c/t:.0f}%")
        seen = set()
        for ci, f, want, noul, state in wrong:
            k = (ci, f)
            if k in seen:
                continue
            seen.add(k)
            print(f"  MISS case{ci} {f}: want={'yes' if want else 'no'} got noul={noul:.3f}  {state}")
        print()


if __name__ == "__main__":
    main()
