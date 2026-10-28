"""彩色方块要货决策回路基准 (语音/文本 -> 规则层 -> Laya -> 校验器 -> (LLM旁路)).

领域: 彩色方块 (红/绿/黄/蓝), 数量 1-99.
语音例: "10个红色方块，5个绿色方块，7个黄色方块"
输出:   [{"color":"red","qty":10},{"color":"green","qty":5},{"color":"yellow","qty":7}]

架构同 bench_industrial.py (水果域): LLM 仅旁路, 校验拒绝才调用;
任何失败安全降级为 暂停+人工. 规则层负责 颜色词+数量 抽取 (ms 级, 确定性),
Laya 负责颜色终审, 校验器做 白名单+数量范围+规则/决策交叉检查.

Usage:
    .venv/bin/python bench_block.py [reps]          # 文本模式, 默认 5 reps
    .venv/bin/python bench_block.py 2 test_audio/block_1.wav  # 语音模式 (单句 wav)
"""

import json
import os
import re
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_pipeline import LAYA, llm_json  # noqa: E402

REPS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
MODE = sys.argv[2] if len(sys.argv) > 2 else "text"

# ---- 冻结颜色白名单 (PLC 可执行的颜色集) ----
COLORS = ["red", "green", "yellow", "blue"]
CN2KEY = {"红": "red", "绿": "green", "黄": "yellow", "蓝": "blue"}
KEY2CN = {v: k for k, v in CN2KEY.items()}
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(tok):
    """10/十/二十/二十三 -> int. 无法解析返回 1 (规则层保守默认)."""
    if tok.isdigit():
        return int(tok)
    if tok in CN_NUM:
        return CN_NUM[tok]
    if "十" in tok:
        pre, _, post = tok.partition("十")
        tens = (CN_NUM.get(pre, 1) * 10) if pre else 10
        return tens + (CN_NUM.get(post, 0) if post else 0)
    return 1


def _qty_before(text, pos):
    pre = text[max(0, pos - 12):pos]
    m = re.search(r"([一两二三四五六七八九十\d]+)\s*(?:个|块|条)?\s*$", pre)
    if not m:
        return 1
    return _cn_num(m.group(1))


_QTY_AFTER = re.compile(r"\s*([一两二三四五六七八九十\d]+)\s*(?:个|块|条)?\s*(?=[，、。！!?,;；\s]|$)")


def _qty_after(text, end):
    """后置数量: "红色方块三个" (ASR 语序). 只在边界处认, 避免吞下一项的数量."""
    m = _QTY_AFTER.match(text[end:])
    return _cn_num(m.group(1)) if m else None


# 否定提及: "不要绿色方块" / "别要红色 方块" / "不要红色的" (ASR 可能丢逗号/插空格)
NEG_RE = re.compile(r"(?:不要|别要?|不需要|不用)\s*"
                    r"(((?:红|绿|黄|蓝)\s*色?\s*(?:的\s*)?(?:方块|块)"
                    r"|(?:红|绿|黄|蓝)\s*色?\s*的(?!\s*(?:苹果|香蕉|梨)))?)")
# 正向提及: 带颜色的 方块/块 | 颜色省略 (X的/X色的, 后不跟水果名) | 裸方块
BLOCK_RE = re.compile(r"((?:红|绿|黄|蓝)\s*色?\s*)(?:的\s*)?(?:方块|块)"
                      r"|((?:红|绿|黄|蓝)\s*色?\s*)的(?!\s*(?:苹果|香蕉|梨))"
                      r"|方块")

# 简繁/同音 -> 领域词 (ASR 噪声: 方塊/紅色/個)
T2S = str.maketrans({"紅": "红", "綠": "绿", "黃": "黄", "藍": "蓝",
                     "塊": "块", "個": "个"})
TIPO = (("方快", "方块"),)  # 双字同音词, maketrans 只收单字符, 用 replace


def _color_of(group):
    if not group:
        return None
    return CN2KEY[re.match(r"(红|绿|黄|蓝)", group).group(1)]


def normalize(text):
    """确定性: 简繁/同音 -> 否定剥离 -> 颜色+数量抽取. 返回 (clean_text, mentions, negated)."""
    text = text.translate(T2S)
    for a, b in TIPO:
        text = text.replace(a, b)
    negated = set()
    for m in NEG_RE.finditer(text):
        negated.add(_color_of(m.group(1)))
    clean = NEG_RE.sub(" ", text)
    mentions = []
    for m in BLOCK_RE.finditer(clean):
        qty = _qty_before(clean, m.start())
        if qty == 1:
            q2 = _qty_after(clean, m.end())
            if q2:
                qty = q2
        mentions.append({"color": _color_of(m.group(1) or m.group(2)),
                         "qty": qty, "negated": False})
    return clean, mentions, sorted(c for c in negated if c)


BLOCK_CRIT = {"red": "红色方块 (red block)", "green": "绿色方块 (green block)",
              "yellow": "黄色方块 (yellow block)", "blue": "蓝色方块 (blue block)"}


def laya_decide(mentions):
    """One Laya request PER mention (同 bench_industrial 的稳定性结论).
    返回 (dt, answers), answers keyed m0..mN-1."""
    answers, t0 = {}, time.perf_counter()
    for i, m in enumerate(mentions):
        color_cn = KEY2CN[m["color"]] + "色" if m["color"] else ""
        body = json.dumps({"state": "用户要货: " + color_cn + "方块 x " + str(m["qty"]),
                           "model": "jev-latest",
                           "questions": {"q": {"type": "choice",
                                               "instructions": "用户要什么颜色的方块?",
                                               "criteria": BLOCK_CRIT}}}).encode()
        req = urllib.request.Request(LAYA, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        answers["m" + str(i)] = d["answers"]["q"]
    return time.perf_counter() - t0, answers


def validate(mentions, answers):
    """确定性白名单 + 交叉检查 + 数量范围. 未指定颜色视为信息不足.
    answers=None (LLM 旁路路径): 白名单 + 范围检查."""
    actions, reasons = [], []
    for i, m in enumerate(mentions):
        if m["color"] is None:
            reasons.append(f"m{i}: 未指定颜色")
            continue
        choice = m["color"]
        if answers is not None:
            a = answers.get("m" + str(i))
            if a is None:
                reasons.append(f"m{i}: 缺少决策输出")
                continue
            choice = a.get("choice")
            if choice not in COLORS:
                reasons.append(f"m{i}: 颜色不在白名单 {choice!r}")
                continue
            if choice != m["color"]:
                reasons.append(f"m{i}: 规则/决策冲突 {m['color']} vs {choice}")
                continue
        if not (1 <= m["qty"] <= 99):
            reasons.append(f"m{i}: 数量越界 {m['qty']}")
            continue
        actions.append({"color": choice, "qty": m["qty"]})
    if not actions and not reasons:
        reasons.append("空订单")
    return (not reasons), actions, reasons


BYPASS_PROMPT = """你是要货解析器。把这句话解析成 JSON, 只输出 JSON:
{{"items":[{{"color":"red|green|yellow|blue","qty":1}}]}}
颜色对应: red=红, green=绿, yellow=黄, blue=蓝.
"不要X" 表示 X 不下单, 不列入 items; 没指定颜色的方块不列入; 数量没说按 1.
句子: {state}"""


def llm_bypass(norm_text, rule_mentions):
    dt, parsed = llm_json(BYPASS_PROMPT.format(state=norm_text))
    if parsed is None or not isinstance(parsed, dict):
        return dt, None, ["LLM 输出无法解析"]
    items = parsed.get("items", [])
    if not isinstance(items, list):
        return dt, None, ["LLM 输出无法解析"]
    mentions, reasons = [], []
    for it in items:
        color = it.get("color", "")
        if color not in COLORS:
            reasons.append(f"LLM 项 {color!r} 不在白名单")
            continue
        qty = it.get("qty", 1)
        if not isinstance(qty, int) or not (1 <= qty <= 99):
            reasons.append(f"LLM 数量 {qty!r} 非法")
            continue
        mentions.append({"color": color, "qty": qty, "negated": False})
    # 幻觉防护: LLM 只能下规则层听到过的颜色
    heard = {m["color"] for m in rule_mentions if m["color"]}
    dropped = [m["color"] for m in mentions if m["color"] not in heard]
    for d in dropped:
        reasons.append(f"LLM 幻觉项(规则层未听到): {d}")
    mentions = [m for m in mentions if m["color"] in heard]
    return dt, mentions, reasons


CASES = [
    ("10个红色方块，5个绿色方块，7个黄色方块",
     [("red", 10), ("green", 5), ("yellow", 7)]),      # 用户原始例子
    ("给我3个蓝色方块和2个红色方块",
     [("blue", 3), ("red", 2)]),
    ("8个黄色方块，不要绿色方块",
     [("yellow", 8)]),
    ("一块红色方块",
     [("red", 1)]),
    ("来两个绿色的 方块",
     [("green", 2)]),
    ("要两个绿色的，再来四个黄色的",
     [("green", 2), ("yellow", 4)]),
    # 超纲: 未指定颜色, 应走 暂停+人工 (expected=None 表示只检查路径)
    ("就给我3个方块", None),
]


def main():
    print(f"mode={MODE} reps={REPS} endpoint={LAYA}")

    if MODE.endswith(".wav"):
        text_mode_cases = False
        try:
            import bench_industrial as bi
            dt, text = bi.asr(MODE)
            print(f"ASR: {dt*1000:.0f} ms  '{text}'\n")
            cases = [(text, None, "report")]
        except Exception as e:
            print(f"ASR 不可用 ({e}), 回退文本模式")
            cases = [(t, e2, "oob" if e2 is None else "scored") for t, e2 in CASES]
            text_mode_cases = True
    else:
        cases = [(t, e, "oob" if e is None else "scored") for t, e in CASES]

    t_laya, t_bypass, t_e2e = [], [], []
    n_fast = n_bypass = n_pause = n_correct = n_total = 0
    for rep in range(REPS):
        for ci, (state_text, expected, kind) in enumerate(cases):
            t0 = time.perf_counter()
            norm, mentions, neg = normalize(state_text)
            dt_l, answers = laya_decide(mentions) if mentions else (0.0, {})
            t_laya.append(dt_l)
            ok, actions, reasons = validate(mentions, answers)
            path, dt_b = "FAST", 0.0
            if not ok:
                try:
                    dt_b, b_mentions, b_reasons = llm_bypass(norm, mentions)
                    t_bypass.append(dt_b)
                    if b_mentions:
                        ok_b, actions, reasons = validate(b_mentions, None)
                    else:
                        ok_b, actions, reasons = False, [], b_reasons or ["LLM 无输出"]
                    path = "BYPASS" if ok_b else "PAUSE+HUMAN"
                except Exception as e:
                    path, actions, reasons = "PAUSE+HUMAN", [], [f"LLM 不可用: {e.__class__.__name__}"]
            e2e = time.perf_counter() - t0
            t_e2e.append(e2e)
            got = frozenset((a["color"], a["qty"]) for a in actions)
            if kind == "report":
                print(f"rep{rep} c{ci} [{path:12s}] (语音, 仅报告) "
                      f"actions={actions}  neg={neg}  reasons={reasons[:2]}")
            else:
                correct = (path == "PAUSE+HUMAN") if kind == "oob" \
                    else (got == frozenset(expected))
                n_total += 1
                n_correct += correct
                if path == "FAST":
                    n_fast += 1
                elif path == "BYPASS":
                    n_bypass += 1
                else:
                    n_pause += 1
                print(f"rep{rep} c{ci} [{path:12s}] {'OK ' if correct else 'WRONG'} "
                      f"{'(超纲, 期望暂停)' if kind == 'oob' else ''} "
                      f"actions={actions}  reasons={reasons[:2]}")
                if not correct:
                    exp = "PAUSE" if kind == "oob" else sorted(expected)
                    print(f"      expect={exp} got={sorted(got)}  state='{state_text}'")

    n = len(t_e2e)
    print(f"\n===== 彩色方块回路汇总 ({MODE}, {REPS} 轮 x {len(cases)} 句) =====")
    print(f"路径: 快路径 {n_fast}   LLM旁路 {n_bypass}   暂停+人工 {n_pause}   (超纲句计入暂停)")
    print(f"正确: {n_correct}/{n_total}")
    if t_laya:
        print(f"Laya:   mean {statistics.mean(t_laya)*1000:.0f} ms  max {max(t_laya)*1000:.0f} ms  (每提及一次请求)")
    if t_bypass:
        print(f"LLM旁路: mean {statistics.mean(t_bypass)*1000:.0f} ms (仅触发时)")
    q = lambda p: sorted(t_e2e)[min(n - 1, int(p * n))] * 1000
    print(f"E2E:    mean {statistics.mean(t_e2e)*1000:.0f} ms  p95 {q(.95):.0f} ms  max {max(t_e2e)*1000:.0f} ms")


if __name__ == "__main__":
    main()
