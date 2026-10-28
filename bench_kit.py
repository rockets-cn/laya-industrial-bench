"""物料-套件订购回路基准 (语音/文本 -> 规则层 -> Laya -> 校验器 -> (LLM旁路)).

领域: 物料-套件 (BOM). "套件"是泛称, 本域实例为 果篮 (类似 bench_yard 的
料场命名: N号 + 名词). 冻结白名单 (PLC 可执行的 套件集 + 物料集):
物料: 香蕉/苹果/梨 (复用本仓库已验证的水果白名单, 苹果不分红绿);
套件按物料清单 (BOM) 展开:
    1号果篮 = 香蕉 + 苹果 + 梨 (各 1)
    2号果篮 = 香蕉 + 梨 (各 1)
语音例: "来一个1号果篮和两个2号果篮"
输出:   {"baskets":[{"basket":"basket_1","qty":1},{"basket":"basket_2","qty":2}],
         "materials":[{"yard":1,"material":"banana","qty":3},
                      {"yard":1,"material":"apple","qty":1},
                      {"yard":1,"material":"pear","qty":3}]}
materials 即 按仓库分组的 PLC 指令 (路由表 物料->仓库号, 本部署全部在 1号仓库).

架构同 bench_yard.py (实时路径不含 LLM):
规则层做 套件(N号果篮) + 物料 + 数量 抽取 (ms 级, 确定性): 套件号/名称/数量
同为白名单 token, 由规则层决定 + 校验器检查; Laya 每提及一次前向: 套件终审
(2 选 1) / 物料终审 (3 选 1), state 带当次提及锚点, 与规则层交叉检查;
校验器做 白名单 + 数量范围 + 交叉检查 + 确定性 BOM 展开 + 同套件/同物料合并;
LLM (qwen3.5) 仅旁路, 任何失败安全降级为 暂停+人工.
超纲 (3号果篮等白名单外套件, 数量越界, 全句否定/无提及) -> 暂停+人工.
裸 "物料" 话题词 (无具体物料/套件名) 不算提及, "物料1号果篮2号果篮" 直接抽套件.

Usage:
    .venv/bin/python bench_kit.py [reps]          # 文本模式, 默认 5 reps
    .venv/bin/python bench_kit.py 2 test_audio/y_1.wav  # 语音模式 (单句 wav)
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

# ---- 冻结白名单 (PLC 可执行的 物料集 + 套件集) ----
MATERIALS = ["banana", "apple", "pear"]
MAT_CN = {"banana": "香蕉", "apple": "苹果", "pear": "梨"}

BASKETS = ["basket_1", "basket_2"]
BASKET_CN = {"basket_1": "1号果篮", "basket_2": "2号果篮"}
BASKET_NUM = {"1": "basket_1", "2": "basket_2", "一": "basket_1", "二": "basket_2"}
# 超纲套件号 (3号果篮等): 命中即 暂停+人工 (一/二 为在纲, 不在此类)
OOB_BASKET_RE = re.compile(r"(?<!\d)([3-9三四五六七八九十])\s*号\s*(?:果篮|果筐|篮子|篮)")
# BOM: 套件 -> {物料: 单位用量}. 确定性展开的唯一依据 (冻结).
BASKET_BOM = {"basket_1": {"banana": 1, "apple": 1, "pear": 1},
              "basket_2": {"banana": 1, "pear": 1}}
# 路由表: 物料 -> 仓库号. 冻结, 每部署一份; Laya 不判仓库号 (同 bench_yard
# 料场号: 基座对 "X号" 数字接地不稳, 由确定性层决定 + 校验器白名单检查).
MAT_YARD = {"banana": 1, "apple": 1, "pear": 1}
YARDS = [1]

# 套件: N号 + 果篮 (汉字/阿拉伯 号码 ASR 变体); 裸 "N号" 不认 (与料场域区分)
BASKET_RE = re.compile(r"(?<!\d)([12一二])\s*号\s*(?:果篮|果筐|篮子|篮)")
# 物料词表 (同 bench_yard / bench_pipeline: 苹果不分红绿)
FRUIT_RE = re.compile(r"(?:红|绿)?苹果|香蕉|梨")

CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 简繁/同音 -> 领域词 (ASR 噪声)
T2S = str.maketrans({"裝": "装", "個": "个", "號": "号", "藍": "蓝", "綠": "绿"})
HOMOPHONE = (("離", "梨"), ("李", "梨"))


def _cn_num(tok):
    """10/十/一百五十/二十三 -> int. 无法解析返回 1 (规则层保守默认). 支持到 百 位."""
    if tok.isdigit():
        return int(tok)
    if tok in CN_NUM:
        return CN_NUM[tok]
    total = 0
    if "百" in tok:
        pre, _, tok = tok.partition("百")
        total += CN_NUM.get(pre, 1) * 100
    if "十" in tok:
        pre, _, post = tok.partition("十")
        total += (CN_NUM.get(pre, 1) * 10) if pre else 10
        total += CN_NUM.get(post, 0) if post else 0
    else:
        total += CN_NUM.get(tok, 0)
    return total or 1


_QTY_BEFORE_TAIL = r"([一两二三四五六七八九十百\d]+)\s*(?:个|块|条|把|根|颗|只|台|套)?"


def _qty_before(text, pos):
    pre = text[max(0, pos - 12):pos]
    m = re.search(_QTY_BEFORE_TAIL + r"\s*$", pre)
    if not m:
        return 1
    return _cn_num(m.group(1))


_QTY_AFTER = re.compile(r"\s*([一两二三四五六七八九十百\d]+)\s*(?:个|块|条|套)?"
                         r"\s*(?=[，、。！!?,;；\s]|$)")


def _qty_after(text, end):
    """后置数量: "1号果篮两个" (ASR 语序). 只在边界处认, 避免吞下一项的数量."""
    m = _QTY_AFTER.match(text[end:])
    return _cn_num(m.group(1)) if m else None


def _fruit_action(name):
    if "苹果" in name:
        return "apple"  # 本域苹果不分红绿 (同 bench_pipeline intent 套件)
    return {"香蕉": "banana", "梨": "pear"}[name]


def _mentions_in(clean):
    """全文 (否定剥离后): 套件/物料 提及+数量, 按位置排序. 返回 [(pos, end, mention)].
    白名单外套件号 (3号果篮等) 记 kind="oob", 由校验器给出明确拒绝理由."""
    found = []
    for m in OOB_BASKET_RE.finditer(clean):
        found.append((m.start(), m.end(),
                       {"kind": "oob", "key": None,
                        "name": m.group(0), "qty": 1}))
    for m in BASKET_RE.finditer(clean):
        key = BASKET_NUM[m.group(1)]
        qty = _qty_before(clean, m.start())
        if qty == 1:
            q2 = _qty_after(clean, m.end())
            if q2:
                qty = q2
        found.append((m.start(), m.end(),
                      {"kind": "basket", "key": key,
                       "name": m.group(0), "qty": qty}))
    for m in FRUIT_RE.finditer(clean):
        key = _fruit_action(m.group(0))
        qty = _qty_before(clean, m.start())
        if qty == 1:
            q2 = _qty_after(clean, m.end())
            if q2:
                qty = q2
        found.append((m.start(), m.end(),
                      {"kind": "mat", "key": key,
                       "name": m.group(0), "qty": qty}))
    found.sort(key=lambda x: x[0])
    return found


NEG_RE = re.compile(r"(?:不要|别要?|不需要|不用)\s*"
                    r"(?:(?:[12一二三])\s*号\s*(?:果篮|果筐|篮子|篮)|(?:红|绿)?苹果|香蕉|梨)")


def normalize(text):
    """确定性: 简繁/同音 -> 否定剥离 -> 套件/物料+数量 全文抽取.
    返回 (clean_text, mentions, negated). 无提及 -> 空提及 (上层暂停);
    命中超纲套件 (3号果篮等) -> 校验器拒."""
    text = text.translate(T2S)
    for a, b in HOMOPHONE:
        text = text.replace(a, b)
    negated = set()
    clean = text
    for m in NEG_RE.finditer(clean):
        negated.add(m.group(0))
    clean = NEG_RE.sub(" ", clean)
    # 否定剥离后重新扫超纲套件 (混合订单里混进 3号果篮 也要整体暂停)
    mentions = [mm for _, _, mm in _mentions_in(clean)]
    return text, mentions, sorted(negated)


BASKET_CRIT = {"basket_1": "1号果篮: 由 香蕉、苹果、梨 各一个组成 (三料果篮)",
                "basket_2": "2号果篮: 由 香蕉、梨 各一个组成 (双料果篮, 不含苹果)"}
MAT_CRIT = {"banana": "香蕉: 黄色, 长条形, 弯曲, 剥皮吃",
            "apple": "苹果: 红色或绿色, 圆形, 表皮光滑, 脆, 甜",
            "pear": "梨: 黄色或黄绿色, 上窄下宽, 多汁"}


def laya_decide(mentions, norm_text=""):
    """One Laya request PER mention: 套件终审 (2 选 1) / 物料终审 (3 选 1), state 带当次提及锚点.
    返回 (dt, answers), answers keyed m{i}."""
    answers, t0 = {}, time.perf_counter()
    for i, m in enumerate(mentions):
        if m["kind"] == "oob" or m.get("key") is None:
            continue  # 信息不足/超纲, 校验器直接给理由, 不再请求
        if m["kind"] == "basket":
            crit, question, label = BASKET_CRIT, "本次下单的果篮是哪一个?", "果篮"
        else:
            crit, question, label = MAT_CRIT, "本次下单的物料是哪种?", "物料"
        name_cn = BASKET_CN[m["key"]] if m["kind"] == "basket" else MAT_CN[m["key"]]
        state = (f"订购指令原文: {norm_text}. 本次下单: {label}{name_cn} x {m['qty']}")
        body = json.dumps({"state": state, "model": "jev-latest",
                           "questions": {"q": {"type": "choice",
                                               "instructions": question,
                                               "criteria": crit}}}).encode()
        req = urllib.request.Request(LAYA, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        answers["m" + str(i)] = d["answers"]["q"]
    return time.perf_counter() - t0, answers


def _expand(baskets, mats):
    """确定性 BOM 展开 + 路由 + 合并: 套件用量 -> 按仓库分组的物料指令.
    materials 即 PLC 指令组: [{yard, material, qty}] (同 bench_yard 输出形状)."""
    totals = {}
    for b in baskets:
        for mat, unit in BASKET_BOM[b["key"]].items():
            totals[mat] = totals.get(mat, 0) + unit * b["qty"]
    for m in mats:
        totals[m["key"]] = totals.get(m["key"], 0) + m["qty"]
    basket_totals = {}
    for b in baskets:
        basket_totals[b["key"]] = basket_totals.get(b["key"], 0) + b["qty"]
    baskets_out = [{"basket": k, "qty": q} for k, q in
                    ((k, basket_totals[k]) for k in BASKETS if k in basket_totals)]
    mats_out = [{"yard": MAT_YARD[k], "material": k, "qty": q} for k, q in
                ((k, totals[k]) for k in MATERIALS if k in totals)]
    return baskets_out, mats_out


def validate(mentions, answers):
    """确定性白名单 + 交叉检查 + 数量范围 + BOM 展开 + 合并.
    answers=None (LLM 旁路路径): 白名单 + 范围检查."""
    baskets, mats, reasons = [], [], []
    for i, m in enumerate(mentions):
        kind, key, qty = m.get("kind"), m.get("key"), m.get("qty", 1)
        if kind == "oob":
            reasons.append(f"m{i}: 超纲套件 {m.get('name')!r} (不在白名单)")
            continue
        if kind == "basket" and key not in BASKETS:
            reasons.append(f"m{i}: 套件越界 {key!r}")
            continue
        if kind == "mat" and key not in MATERIALS:
            reasons.append(f"m{i}: 物料越界 {key!r}")
            continue
        if answers is not None:
            a_m = answers.get("m" + str(i))
            if a_m is None:
                reasons.append(f"m{i}: 缺少决策输出")
                continue
            cm = a_m.get("choice")
            if cm != key:
                reasons.append(f"m{i}: 规则/决策冲突 {key} vs {cm}")
                continue
        if not (1 <= qty <= 999):
            reasons.append(f"m{i}: 数量越界 {qty}")
            continue
        (baskets if kind == "basket" else mats).append({"key": key, "qty": qty})
    if not (baskets or mats) and not reasons:
        reasons.append("空订单 (无套件/物料提及或全部被否定)")
    ok = not reasons
    baskets_out, mats_out = (_expand(baskets, mats) if ok else ([], []))
    result = {"baskets": baskets_out, "materials": mats_out}
    return ok, result, reasons


BYPASS_PROMPT = """你是物料-果篮订购解析器。把这句话解析成 JSON, 只输出 JSON, 不要其他文字。
套件白名单: basket_1=1号果篮 (香蕉+苹果+梨 各1), basket_2=2号果篮 (香蕉+梨 各1).
物料白名单: banana=香蕉, apple=苹果, pear=梨.
输出格式: {{"baskets":[{{"basket":"basket_1","qty":1}}], "materials":[{{"material":"banana","qty":1}}]}}
"不要X" 表示 X 不下单, 不列入; 数量没说按 1; 白名单外名称 (3号果篮等) 一律不列入.
句子: {state}"""


def llm_bypass(norm_text, rule_mentions):
    # 越界守卫: 规则层听到 超纲名称/越界数量 -> 整体暂停, 旁路不救部分单
    # (4B LLM 会把 "两百" 读成 2 等; 越界一律 暂停+人工, 与 bench_yard 整体暂停
    # 语义一致). 在 LLM 调用前检查: 超纲句 0 ms 暂停, 不烧 10s 旁路.
    for m in rule_mentions:
        if m.get("kind") == "oob" or not (1 <= m.get("qty", 1) <= 999):
            return 0.0, None, [f"规则层越界: {m.get('name')!r} 数量={m.get('qty')}, 整体暂停"]
    dt, parsed = llm_json(BYPASS_PROMPT.format(state=norm_text))
    if parsed is None or not isinstance(parsed, dict):
        return dt, None, ["LLM 输出无法解析"]
    mentions, reasons = [], []
    for it in parsed.get("baskets", []) or []:
        key = it.get("basket", "")
        if key not in BASKETS:
            reasons.append(f"LLM 项 {key!r} 不在套件白名单")
            continue
        qty = it.get("qty", 1)
        if not isinstance(qty, int) or not (1 <= qty <= 999):
            reasons.append(f"LLM 数量 {qty!r} 非法")
            continue
        mentions.append({"kind": "basket", "key": key, "qty": qty})
    for it in parsed.get("materials", []) or []:
        key = it.get("material", "")
        if key not in MATERIALS:
            reasons.append(f"LLM 项 {key!r} 不在物料白名单")
            continue
        qty = it.get("qty", 1)
        if not isinstance(qty, int) or not (1 <= qty <= 99):
            reasons.append(f"LLM 数量 {qty!r} 非法")
            continue
        mentions.append({"kind": "mat", "key": key, "qty": qty})
    # 幻觉防护: LLM 只能下 规则层听到过的 套件/物料
    heard = {m.get("key") for m in rule_mentions if m.get("key")}
    mentions = [m for m in mentions if m["key"] in heard]
    return dt, mentions or None, reasons


CASES = [
    # (state, expected {"baskets":{b:qty}, "materials":{m:qty}} | None=超纲暂停)
    ("来一个1号果篮和两个2号果篮",
     {"baskets": {"basket_1": 1, "basket_2": 2},
      "materials": {"banana": 3, "apple": 1, "pear": 3}}),       # BOM 展开
    ("1号果篮，2号果篮",
     {"baskets": {"basket_1": 1, "basket_2": 1},
      "materials": {"banana": 2, "apple": 1, "pear": 2}}),       # 缺省数量=1
    ("物料1号果篮2号果篮",
     {"baskets": {"basket_1": 1, "basket_2": 1},
      "materials": {"banana": 2, "apple": 1, "pear": 2}}),      # 裸"物料"话题词
    ("两套2号果篮",
     {"baskets": {"basket_2": 2},
      "materials": {"banana": 2, "pear": 2}}),                    # 量词"套"
    ("一个1号果篮加一个梨",
     {"baskets": {"basket_1": 1},
      "materials": {"banana": 1, "apple": 1, "pear": 2}}),      # 直订并入
    ("三个梨",
     {"baskets": {}, "materials": {"pear": 3}}),                 # 纯物料订单
    # 汉字号码 ASR 变体
    ("一号果篮一个2号果篮",
     {"baskets": {"basket_1": 1, "basket_2": 1},
      "materials": {"banana": 2, "apple": 1, "pear": 2}}),
    # 无标点密集句 (真实语音)
    ("一个1号果篮两个梨一个2号果篮",
     {"baskets": {"basket_1": 1, "basket_2": 1},
      "materials": {"banana": 2, "apple": 1, "pear": 4}}),
    # 大批量组篮: 确定性 BOM 展开 100x (下游 PLC 按组发料)
    ("要组100个1号果篮",
     {"baskets": {"basket_1": 100},
      "materials": {"banana": 100, "apple": 100, "pear": 100}}),
    # 超纲: 应走 暂停+人工 (expected=None)
    ("来一个3号果篮", None),                             # 套件白名单外
    ("来1000个1号果篮", None),                           # 数量越界
    ("不要1号果篮，不要香蕉", None),                     # 全否定 -> 空订单
    ("今天天气不错", None),                              # 无提及
]


def _expected_cmp(result, expected):
    got = {"baskets": {k["basket"]: k["qty"] for k in result.get("baskets", [])},
           "materials": {k["material"]: k["qty"] for k in result.get("materials", [])}}
    yard_ok = all(k.get("yard") == MAT_YARD.get(k.get("material"))
                  for k in result.get("materials", []))
    return got == expected and yard_ok


def main():
    print(f"mode={MODE} reps={REPS} endpoint={LAYA}")
    print("BOM:", json.dumps(BASKET_BOM, ensure_ascii=False))

    if MODE.endswith(".wav"):
        try:
            import bench_industrial as bi
            dt, text = bi.asr(MODE)
            print(f"ASR: {dt*1000:.0f} ms  '{text}'\n")
            cases = [(text, None, "report")]
        except Exception as e:
            print(f"ASR 不可用 ({e}), 回退文本模式")
            cases = [(t, e2, "oob" if e2 is None else "scored") for t, e2 in CASES]
    else:
        cases = [(t, e, "oob" if e is None else "scored") for t, e in CASES]

    t_laya, t_bypass, t_e2e = [], [], []
    n_fast = n_bypass = n_pause = n_correct = n_total = 0
    for rep in range(REPS):
        for ci, (state_text, expected, kind) in enumerate(cases):
            t0 = time.perf_counter()
            norm, mentions, neg = normalize(state_text)
            dt_l, answers = laya_decide(mentions, norm) if mentions else (0.0, {})
            t_laya.append(dt_l)
            ok, result, reasons = validate(mentions, answers)
            path, dt_b = "FAST", 0.0
            if not ok:
                try:
                    dt_b, b_mentions, b_reasons = llm_bypass(norm, mentions)
                    t_bypass.append(dt_b)
                    if b_mentions:
                        ok_b, result, reasons = validate(b_mentions, None)
                    else:
                        ok_b, result, reasons = (False, {"baskets": [], "materials": []},
                                                   b_reasons or ["LLM 无输出"])
                    path = "BYPASS" if ok_b else "PAUSE+HUMAN"
                except Exception as e:
                    path = "PAUSE+HUMAN"
                    result, reasons = {"baskets": [], "materials": []}, [f"LLM 不可用: {e.__class__.__name__}"]
            e2e = time.perf_counter() - t0
            t_e2e.append(e2e)
            if kind == "report":
                print(f"rep{rep} c{ci} [{path:12s}] (语音, 仅报告) "
                      f"result={result}  neg={neg}  reasons={reasons[:2]}")
            else:
                correct = (path == "PAUSE+HUMAN") if kind == "oob" \
                    else _expected_cmp(result, expected)
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
                      f"baskets={result['baskets']} materials={result['materials']}  "
                      f"reasons={reasons[:2]}")
                if not correct:
                    exp = "PAUSE" if kind == "oob" else expected
                    print(f"      expect={exp}  state='{state_text}'")

    n = len(t_e2e)
    print(f"\n===== 物料-果篮回路汇总 ({MODE}, {REPS} 轮 x {len(cases)} 句) =====")
    print(f"路径: 快路径 {n_fast}   LLM旁路 {n_bypass}   暂停+人工 {n_pause}   (超纲句计入暂停)")
    print(f"正确: {n_correct}/{n_total}")
    if t_laya:
        print(f"Laya:   mean {statistics.mean(t_laya)*1000:.0f} ms  max {max(t_laya)*1000:.0f} ms  (每提及 1 次请求: 套件/物料终审)")
    if t_bypass:
        print(f"LLM旁路: mean {statistics.mean(t_bypass)*1000:.0f} ms (仅触发时)")
    q = lambda p: sorted(t_e2e)[min(n - 1, int(p * n))] * 1000
    print(f"E2E:    mean {statistics.mean(t_e2e)*1000:.0f} ms  p95 {q(.95):.0f} ms  max {max(t_e2e)*1000:.0f} ms")


if __name__ == "__main__":
    main()
