"""Pipeline benchmark: LLM (qwen3.5:4b via ollama) intent organization -> Laya final decision.

Two suites:
  1. intent: 4 multi-intent order sentences -> LLM parses to
     {intents:[{item,want,qty}]} -> Laya classifies each item into the 4 fruit
     categories. Compared against pure-Laya (known: 58.3% question accuracy).
  2. fruit:  21 single-intent descriptions -> LLM directly classifies into the
     4 categories. Compared against pure-Laya base (66.7%) / rich (71.4%).

Reports per-stage and end-to-end latency + accuracy.

Usage: .venv/bin/python bench_pipeline.py [intent_reps] [fruit_reps]
"""

import json
import statistics
import sys
import time
import urllib.request

import os

OLLAMA = "http://127.0.0.1:11434/api/generate"
LAYA = "http://127.0.0.1:8000/v1/systemone"
MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b")
INTENT_REPS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
FRUIT_REPS = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3

FRUIT_CRIT = {"red_apple": "红苹果: 红色, 圆形, 表皮光滑, 脆甜",
              "green_apple": "绿苹果: 绿色, 圆形, 脆, 略酸",
              "banana": "黄香蕉: 黄色, 长条形, 弯曲, 剥皮吃",
              "pear": "梨: 黄色或黄绿色, 上窄下宽, 多汁"}

# --- intent suite: (state, expected {fruit: (want, qty or None)}) ---
INTENT_CASES = [
    ("我要那个苹果，我要那个梨，给我十个香蕉",
     {"red_apple": (True, 1), "pear": (True, 1), "banana": (True, 10)}),
    ("来两个红苹果和一把香蕉",
     {"red_apple": (True, 2), "banana": (True, None)}),
    ("只给我三个梨就行",
     {"pear": (True, 3)}),
    ("一个苹果，不要梨，五个香蕉谢谢",
     {"red_apple": (True, 1), "pear": (False, None), "banana": (True, 5)}),
]

# --- fruit suite: (state, expected class) ---
FRUIT_CASES = [
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
    ("A shiny red round fruit, crisp and sweet when bitten.", "red_apple"),
    ("I bit into a green round fruit; it was crisp and a bit sour.", "green_apple"),
    ("He peeled a long curved yellow fruit; the flesh was soft inside.", "banana"),
    ("The fruit is yellow-green, wider at the bottom than the top, and very juicy.", "pear"),
    ("A round red fruit with a short stem on top, smells like orchard fruit.", "red_apple"),
    ("A ball-sized green fruit, crunchy and tart.", "green_apple"),
    ("A yellow banana-shaped fruit, peeled in one long strip.", "banana"),
    ("A pear-shaped fruit, yellowish, dripping with juice.", "pear"),
]

INTENT_PROMPT = """你是意图解析器。把用户的话解析成 JSON, 只输出 JSON, 不要其他文字。
水果类别只能是: 苹果, 梨, 香蕉 (苹果不区分红绿)。
输出格式: {{"intents":[{{"item":"苹果","want":true,"qty":1}}]}}
want=false 表示用户明确不要; qty 是数量, 没说数量填 1, want=false 时填 null。
用户的话: {state}"""

FRUIT_PROMPT = """把下面描述分类为四种水果之一, 只输出 JSON:
{{"fruit": "red_apple|green_apple|banana|pear"}}
描述: {state}"""


def http_post(url, body, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return time.perf_counter() - t0, data


def llm_json(prompt, timeout=300):
    dt, d = http_post(OLLAMA, {"model": MODEL, "prompt": prompt, "stream": False,
                                "think": False,
                                "options": {"temperature": 0, "num_predict": 300}}, timeout)
    txt = d.get("response", "")
    s, e = txt.find("{"), txt.rfind("}")
    return dt, json.loads(txt[s:e + 1]) if s >= 0 and e > s else None


def laya_choice(state, criteria):
    dt, d = http_post(LAYA, {"state": state, "model": "jev-latest",
                             "questions": {"fruit": {"type": "choice",
                                                       "instructions": "这是哪种水果?",
                                                       "criteria": criteria}}})
    return dt, d["answers"]["fruit"]


def pct(c, t):
    return f"{c}/{t} = {100*c/t:.0f}%" if t else "n/a"


def main():
    print(f"model={MODEL}  intent_reps={INTENT_REPS} fruit_reps={FRUIT_REPS}")
    dt, _ = llm_json(FRUIT_PROMPT.format(state=FRUIT_CASES[0][0]))
    print(f"warmup (first LLM call): {dt:.1f}s\n")

    # ===== Suite 1: intent pipeline =====
    print("===== 套件1: 多意图订单 (LLM 整理 -> Laya 终审) =====")
    llm_lat, laya_lat, pipe_lat = [], [], []
    item_ok = item_tot = want_ok = want_tot = qty_ok = qty_tot = 0
    seen = set()
    for rep in range(INTENT_REPS):
        for ci, (state, exp) in enumerate(INTENT_CASES):
            t0 = time.perf_counter()
            dt_llm, parsed = llm_json(INTENT_PROMPT.format(state=state))
            llm_lat.append(dt_llm)
            if parsed is None:
                want_tot += len(exp); item_tot += len(exp)
                print(f"  !! case{ci} rep{rep}: LLM output unparseable")
                continue
            parsed_items = {it["item"]: it for it in parsed.get("intents", [])}
            dt_laya = 0.0
            got_map = {}
            for name, it in parsed_items.items():
                cls = {"苹果": "red_apple", "梨": "pear", "香蕉": "banana"}[name]
                d2, a = laya_choice(f"水果: {name}。用户要求: 数量{it.get('qty')}", FRUIT_CRIT)
                dt_laya += d2
                got_map[name] = a["choice"]
            laya_lat.append(dt_laya)
            pipe_lat.append(time.perf_counter() - t0)
            for fruit, (want, qty) in exp.items():
                name = {v: k for k, v in {"苹果": "red_apple", "梨": "pear", "香蕉": "banana"}.items()}[fruit]
                it = parsed_items.get(name)
                if it is None:
                    want_tot += 1
                else:
                    want_tot += 1
                    want_ok += (bool(it.get("want")) == want)
                    item_tot += 1
                    got = got_map.get(name)
                    item_ok += (got == fruit)
                    if want and qty is not None:
                        qty_tot += 1
                        q = it.get("qty")
                        qty_ok += (q == qty)
                    k = (ci, name)
                    if not (bool(it.get("want")) == want) and k not in seen:
                        seen.add(k)
                        print(f"  MISS want: case{ci} {name}: want={want} got={it}  {state}")
    print(f"LLM parse latency:    mean {statistics.mean(llm_lat)*1000:.0f} ms  "
          f"p95 {sorted(llm_lat)[int(.95*len(llm_lat))]*1000:.0f} ms  max {max(llm_lat)*1000:.0f} ms")
    if laya_lat:
        print(f"Laya finalize:      mean {statistics.mean(laya_lat)*1000:.0f} ms  max {max(laya_lat)*1000:.0f} ms")
    print(f"end-to-end/句:        mean {statistics.mean(pipe_lat)*1000:.0f} ms  "
          f"p95 {sorted(pipe_lat)[int(.95*len(pipe_lat))]*1000:.0f} ms  max {max(pipe_lat)*1000:.0f} ms")
    print(f"want 判断准确率:  {pct(want_ok, want_tot)}   (纯Laya基线: 58%)")
    print(f"item 分类准确率:  {pct(item_ok, item_tot)}   (LLM项 -> Laya归入4类)")
    print(f"qty   准确率:     {pct(qty_ok, qty_tot)}   (纯Laya: 只能判是否十个, 无数字)")
    print()

    # ===== Suite 2: fruit classification, LLM direct vs Laya =====
    print(f"===== 套件2: 水果描述分类 (21 条 x {FRUIT_REPS} 轮, 纯LLM) =====")
    lat, per = [], {}
    seen = set()
    for rep in range(FRUIT_REPS):
        for ci, (state, exp) in enumerate(FRUIT_CASES):
            dt, parsed = llm_json(FRUIT_PROMPT.format(state=state))
            lat.append(dt)
            got = parsed["fruit"] if parsed and "fruit" in (parsed or {}) else "PARSE_ERR"
            d = per.setdefault(exp, [0, 0])
            d[1] += 1
            d[0] += (got == exp)
            if got != exp and ci not in seen:
                seen.add(ci)
                print(f"  MISS case{ci}: expect {exp} got {got}  {state[:40]}")
    lat_ms = sorted(x * 1000 for x in lat)
    total_c = sum(c for c, _ in per.values()); total_t = sum(t for _, t in per.values())
    print(f"latency: mean {statistics.mean(lat_ms):.0f} ms  p95 {lat_ms[int(.95*len(lat_ms))]:.0f} ms  "
          f"max {lat_ms[-1]:.0f} ms")
    print(f"overall: {pct(total_c, total_t)}   (纯Laya: base 66.7% / rich 71.4%)")
    for cls in ("red_apple", "green_apple", "banana", "pear"):
        c, t = per.get(cls, (0, 0))
        print(f"  {cls:12s} {pct(c, t)}")


if __name__ == "__main__":
    main()
