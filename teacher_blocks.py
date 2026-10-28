"""彩色方块域 离线教师 (qwen3.5:4b via Ollama): 新增领域时 LLM 的贡献.

职责 (全部离线, 不进实时路径, 同 teacher_offline.py 的设计原则):
  1. 生成: 规则模板句 (已知 gold) + LLM 新句 (口语化, 含水果混入/否定/ASR 噪声)
  2. 交叉标注: qwen4b 对每句独立解析 {color, qty}
  3. 校验规则层: 用 qwen4b 的标签对照 bench_block.normalize() 的抽取结果,
     一致率 + mismatch 明细 -> 定位 正则 的盲区
  4. 产出: teacher/blocks_corpus.jsonl (可直接用于后续 Laya 微调, 同
     teacher/train.jsonl 的用法)

Usage: .venv/bin/python teacher_blocks.py [n_llm]      # 默认 30 条 LLM 新句
"""

import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_pipeline import OLLAMA, http_post  # noqa: E402
from bench_block import normalize, COLORS  # noqa: E402


def ollama_json(prompt, num_predict=512):
    """Ollama -> 解析首个 JSON 对象/数组. 同 teacher_offline: 默认 think=false
    (本机 4b 的 thinking 不受控, 标签是确定性短任务)."""
    dt, d = http_post(OLLAMA, {"model": os.environ.get("LLM_MODEL", "qwen3.5:4b"),
                                "prompt": prompt, "stream": False, "think": False,
                                "options": {"temperature": 0, "num_predict": num_predict,
                                            "num_gpu": 99}})
    txt = d.get("response", "")
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = txt.find(open_c), txt.rfind(close_c)
        if s >= 0 and e > s:
            try:
                return dt, json.loads(txt[s:e + 1])
            except json.JSONDecodeError:
                pass
    return dt, None

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "teacher", "blocks_corpus.jsonl")
N_LLM = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30

COLORS_CN = {"red": "红", "green": "绿", "yellow": "黄", "blue": "蓝"}
QTY_ZH = ["一", "两", "三", "五", "八", "十", "二十"]
QTY_DIG = ["1", "3", "6", "10", "15", "20"]


def zh2int(tok):
    from bench_block import _cn_num
    return _cn_num(tok)


# ---- 1. 规则模板句 (gold 已知) ----
def gen_templates(seed=7):
    rng = random.Random(seed)
    out = []

    def one(qty, c, style):
        c_cn = COLORS_CN[c]
        if style == 0:
            return f"{qty}个{c_cn}色方块"
        if style == 1:
            return f"{qty}个{c_cn}方块"
        if style == 2:
            return f"{qty}{c_cn}色方块"
        return f"{qty}个{c_cn}色的方块"

    # 单提及 x 数量形式 x 颜色
    for c in COLORS_CN:
        for q in QTY_ZH[:4] + QTY_DIG[:3]:
            out.append((one(q, c, 0), {c: int(q) if q.isdigit() else zh2int(q)}))
    # 多样句式
    for i in range(8):
        c, q = rng.choice(list(COLORS_CN)), rng.choice(QTY_ZH + QTY_DIG)
        out.append((one(q, c, rng.randint(1, 3)), {c: int(q) if q.isdigit() else zh2int(q)}))
    # 多提及 (2-3 种颜色, 用 和/，)
    for i in range(10):
        cs = rng.sample(list(COLORS_CN), rng.choice([2, 3]))
        qs = [str(rng.choice(QTY_ZH + QTY_DIG)) for _ in cs]
        parts = [one(q, c, rng.randint(0, 1)) for q, c in zip(qs, cs)]
        text = ("，" if rng.random() < 0.5 else "和").join(parts)
        out.append((text, {c: (int(q) if q.isdigit() else zh2int(q)) for q, c in zip(qs, cs)}))
    # 否定
    for i in range(6):
        c, c2 = rng.sample(list(COLORS_CN), 2)
        q = rng.choice(QTY_ZH + QTY_DIG)
        out.append((f"{one(q, c, 0)}，不要{COLORS_CN[c2]}色方块",
                    {c: int(q) if q.isdigit() else zh2int(q)}))
    # 混入水果 (水果域仍在用, 方块抽取必须不受干扰)
    FR = ["两个红苹果", "一把香蕉", "三个梨", "一个苹果"]
    for i in range(8):
        c = rng.choice(list(COLORS_CN))
        q = rng.choice(QTY_ZH + QTY_DIG)
        fr = rng.choice(FR)
        text = (one(q, c, 0) + "，" + fr) if rng.random() < 0.5 else (fr + "和" + one(q, c, 0))
        out.append((text, {c: int(q) if q.isdigit() else zh2int(q)}))
    # ASR 噪声: 繁体/错字/空格
    out.append(("方塊要紅色方塊三個", {"red": 3}))
    out.append(("5 個 黄色 方块", {"yellow": 5}))
    out.append(("来方快两个 蓝色的", {"blue": 2}))
    return out


GEN_PROMPT = """你是语音指令生成器。生成 {n} 条工厂工人下达的"彩色方块要货"口语指令。
只输出 JSON 字符串数组, 不要解释。
要求: 颜色限 红/绿/黄/蓝; 每句 1-3 种颜色; 数量 1-30, 可说 个/块/条, 中文数字或阿拉伯数字;
可混入 1 种水果要货 (苹果/香蕉/梨, 带红/绿/黄修饰); 约 1/4 的句子带 "不要X"; 口语化。"""


def gen_llm(n):
    per = 10
    out, seen = [], set()
    for _ in range(max(1, -(-n // per))):
        want = min(per, n - len(out))
        _, parsed = ollama_json(GEN_PROMPT.format(n=want), num_predict=4096)
        if isinstance(parsed, list):
            for s in parsed:
                if isinstance(s, str) and len(s) >= 4 and len(out) < n and s not in seen:
                    seen.add(s)
                    out.append(s)
    return out


LABEL_PROMPT = """你是要货解析器。从这句话里只提取"彩色方块"的下单项, 忽略水果和其他内容。
只输出 JSON: {{"items":[{{"color":"red|green|yellow|blue","qty":1}}]}}
颜色对应: red=红, green=绿, yellow=黄, blue=蓝.
"不要X" 不列入; 没指定颜色的方块不列入; 数量没说按 1.
句子: {state}"""


def llm_items(state):
    _, parsed = ollama_json(LABEL_PROMPT.format(state=state), num_predict=512)
    if not isinstance(parsed, dict):
        return None
    items = parsed.get("items", [])
    if not isinstance(items, list):
        return None
    ok = []
    for it in items:
        c = it.get("color") if isinstance(it, dict) else None
        q = it.get("qty") if isinstance(it, dict) else None
        if c in COLORS and isinstance(q, int) and 1 <= q <= 99:
            ok.append((c, q))
    return ok


def main():
    print(f"teacher=qwen3.5:4b (Ollama)  LLM新句目标={N_LLM}")

    templates = gen_templates()
    print(f"模板句: {len(templates)}")
    t0 = time.perf_counter()
    llm_texts = gen_llm(N_LLM)
    print(f"LLM 新句: {len(llm_texts)}  (生成 {time.perf_counter()-t0:.0f}s)")

    all_cases = [(t, g, "template") for t, g in templates] + \
                [(t, None, "llm") for t in llm_texts]
    print(f"\n交叉标注 {len(all_cases)} 句 (qwen4b, 逐句)...")
    rows, agree, tot, mism = [], 0, 0, []
    for i, (text, gold, kind) in enumerate(all_cases):
        llm_items_ = llm_items(text)
        _, mentions, neg = normalize(text)
        rule_items = frozenset((m["color"], m["qty"]) for m in mentions if m["color"])
        llm_frozen = frozenset(llm_items_) if llm_items_ is not None else None
        row = {"text": text, "kind": kind, "gold": gold,
               "llm": sorted(llm_frozen) if llm_frozen is not None else None,
               "rule": sorted(rule_items),
               "neg": neg, "rule_parse_fail": llm_frozen is None}
        rows.append(row)
        if llm_frozen is None:
            continue
        if kind == "template" and gold is not None:
            # qwen4b 对已知 gold 的准确率 (教师可信度)
            pass
        tot += 1
        ok = llm_frozen == rule_items
        agree += ok
        if not ok:
            mism.append(row)
        if i % 20 == 0 and i:
            print(f"  {i}/{len(all_cases)} ...")

    # 教师可信度: qwen4b 在 模板句 (已知gold) 上的表现
    tmpl = [r for r in rows if r["kind"] == "template" and r["llm"] is not None]
    t_ok = sum(1 for r in tmpl if frozenset(r["llm"]) == frozenset(r["gold"].items()))
    print(f"\n===== 教师可信度 (qwen4b vs 模板句已知gold) =====")
    print(f"  {t_ok}/{len(tmpl)} = {100*t_ok/len(tmpl):.1f}%")
    bad_t = [r for r in tmpl if frozenset(r["llm"]) != frozenset(r["gold"].items())]
    for r in bad_t[:5]:
        print(f"  [teacher-miss] '{r['text']}'  gold={r['gold']}  llm={r['llm']}")

    print(f"\n===== 规则层 vs qwen4b 交叉校验 =====")
    print(f"  一致率: {agree}/{tot} = {100*agree/tot:.1f}%")
    for r in mism[:12]:
        print(f"  [mismatch] '{r['text']}'")
        print(f"      llm={r['llm']}  rule={r['rule']}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n语料已存: {OUT}  ({len(rows)} 句)")
    print("用法: 同 teacher/train.jsonl —— 人工过一遍 mismatch 后, 可加入 Laya 微调数据集")


if __name__ == "__main__":
    main()
