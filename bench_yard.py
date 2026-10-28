"""料场分拣决策回路基准 (语音/文本 -> 规则层 -> Laya -> 校验器 -> (LLM旁路)).

领域: 料场分拣. 三个料场 (1号/2号/3号), 物料复用本仓库已验证的两套白名单
(水果: 红苹果/绿苹果/香蕉/梨; 方块: 红/绿/黄/蓝 方块), 共 8 种物料.
语音例: "给1号料场送两个红苹果，3号料场送10个蓝色方块"
输出:   [{"yard":1,"material":"red_apple","qty":2},
         {"yard":3,"material":"blue_block","qty":10}]

架构同 bench_industrial.py / bench_block.py (实时路径不含 LLM):
规则层做 料场号 + 物料 + 数量 抽取 (ms 级, 确定性): 料场号与数量同为白名单
数值, 由规则层决定 + 校验器检查 (实测基座模型对 "X号" 数字接地不稳, 不终审);
Laya 每提及一次前向: 物料终审 (8 选 1, state 带当次提及锚点, 实测 conf≈1.0),
与规则层交叉检查; 校验器做 料场白名单 + 物料白名单 + 数量范围 + 交叉检查,
同料场同物料合并; LLM (qwen3.5) 仅旁路, 任何失败安全降级为 暂停+人工.

Usage:
    .venv/bin/python bench_yard.py [reps]          # 文本模式, 默认 5 reps
    .venv/bin/python bench_yard.py 2 test_audio/y_1.wav  # 语音模式 (单句 wav)
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

# ---- 冻结白名单 (PLC 可执行的 料场集 + 物料集) ----
YARDS = [1, 2, 3]
YARD2CN = {1: "一", 2: "二", 3: "三"}
YARD_NUM = {"1": 1, "2": 2, "3": 3, "一": 1, "二": 2, "三": 3, "幺": 1, "壹": 1}
# 超纲料场 token (4号/四号/11号 ...): 命中即 暂停+人工
OOB_YARD_RE = re.compile(r"(?<!\d)([4-9]\d*|[四五六七八九十])\s*号")

MATERIALS = ["red_apple", "green_apple", "banana", "pear",
             "red_block", "green_block", "yellow_block", "blue_block"]
MAT_CN = {"red_apple": "红苹果", "green_apple": "绿苹果", "banana": "香蕉", "pear": "梨",
          "red_block": "红色方块", "green_block": "绿色方块",
          "yellow_block": "黄色方块", "blue_block": "蓝色方块"}
CN2KEY = {"红": "red", "绿": "green", "黄": "yellow", "蓝": "blue"}
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 在纲料场: 1号/2号/3号 + 汉字/幺号 (ASR 变体), 后接 料场/场地/仓库/库 可选
YARD_RE = re.compile(r"(?<!\d)([123一二三幺壹])\s*号(?:\s*(?:仓库|料场|场地|库|位置|区))?")

# 物料词表 (同 bench_industrial / bench_block 的既有模式)
FRUIT_RE = re.compile(r"(?:红|绿)?苹果|香蕉|梨")
BLOCK_RE = re.compile(r"((?:红|绿|黄|蓝)\s*色?\s*)(?:的\s*)?(?:方块|块)"
                      r"|((?:红|绿|黄|蓝)\s*色?\s*)的(?!红苹果|绿苹果|苹果|香蕉|梨)"
                      r"|方块")
# 否定提及 (同 bench_block / bench_industrial)
BLOCK_NEG_RE = re.compile(r"(?:不要|别要?|不需要|不用)\s*"
                          r"(((?:红|绿|黄|蓝)\s*色?\s*(?:的\s*)?(?:方块|块)"
                          r"|(?:红|绿|黄|蓝)\s*色?\s*的(?!\s*(?:苹果|香蕉|梨)))?)")

# 简繁/同音 -> 领域词 (ASR 噪声)
T2S = str.maketrans({"紅": "红", "綠": "绿", "黃": "黄", "藍": "蓝",
                     "塊": "块", "個": "个", "離": "离", "號": "号"})
HOMOPHONE = (("离", "梨"), ("李", "梨"), ("禮", "梨"), ("方快", "方块"))


def _cn_num(tok):
    """10/十/二十三 -> int. 无法解析返回 1 (规则层保守默认)."""
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
    m = re.search(r"([一两二三四五六七八九十\d]+)\s*(?:个|块|条|把|根|颗|只|台)?\s*$", pre)
    if not m:
        return 1
    return _cn_num(m.group(1))


_QTY_AFTER = re.compile(r"\s*([一两二三四五六七八九十\d]+)\s*(?:个|块|条)?\s*(?=[，、。！!?,;；\s]|$)")


def _qty_after(text, end):
    """后置数量: "红色方块三个" (ASR 语序). 只在边界处认, 避免吞下一项的数量."""
    m = _QTY_AFTER.match(text[end:])
    return _cn_num(m.group(1)) if m else None


def _color_of(group):
    if not group:
        return None
    m = re.match(r"(红|绿|黄|蓝)", group)
    return CN2KEY[m.group(1)] if m else None


def _fruit_action(name):
    if "苹果" in name:
        if name.startswith("红"):
            return "red_apple"
        if "绿" in name:
            return "green_apple"
        return "red_apple"  # 裸"苹果" -> PLC 默认 (同 bench_industrial)
    return {"香蕉": "banana", "梨": "pear"}[name]


def _materials_in(seg):
    """全文 (否定剥离后): 水果/方块 提及+数量, 按位置排序. 返回 [(pos, end, mention)]"""
    found = []
    for m in FRUIT_RE.finditer(seg):
        found.append((m.start(), m.end(), {"material": _fruit_action(m.group(0)),
                                           "qty": _qty_before(seg, m.start())}))
    for m in BLOCK_RE.finditer(seg):
        color = _color_of(m.group(1) or m.group(2))
        qty = _qty_before(seg, m.start())
        if qty == 1:
            q2 = _qty_after(seg, m.end())
            if q2:
                qty = q2
        found.append((m.start(), m.end(),
                      {"material": (color + "_block") if color else None,
                       "qty": qty}))
    found.sort(key=lambda x: x[0])
    return found


def _nearest_yard(matches, pos):
    """提及归属最近的料场标记 (距离到标记 span); 等距时归后一个料场.
    现在仅作兜底: 子句内多标记 / 子句无标记 时使用 (主归属见 normalize 的
    子句切分). '两个红苹果送到3号，五个香蕉给1号' 类倒序句式由子句切分处理."""
    best = None
    for m in matches:
        if m.start() <= pos <= m.end():
            d = 0
        elif pos < m.start():
            d = m.start() - pos
        else:
            d = pos - m.end()
        if best is None or d < best[0] or (d == best[0] and m.start() > best[1].start()):
            best = (d, m)
    if best is None:
        return 0  # 不可达: 调用方保证至少一个料场标记; 防御性降级 -> 校验器拒
    return YARD_NUM[best[1].group(1)]


def _is_reverse_attach(clean, mat_end, marker_start):
    """物料尾与下一个料场标记之间是否只有 标点/空格 + 短运送动词
    ('两个苹果放到二号' / '香蕉给1号' 倒序黏附信号). 空间隔/纯标点 = 正序."""
    gap = clean[mat_end:marker_start]
    if len(gap) > 4:
        return False
    if not re.fullmatch(r"[\s，,。；;！!？?]*(?:送到|送去|送|给|放|进|到|来)?[\s，,。；;！!？?]*",
                        gap):
        return False
    return any(v in gap for v in "送给放进来到")


def _yard_at(clean, cms, pos, mat_end):
    """物料归属: 正序分域为主 — 料场标记拥有 [自身起点, 下一标记起点) 的物料,
    第一个标记之前的内容归第一个标记. 无标点密集句也成立
    ('一号仓库一个香蕉两个苹果二号仓库…' 中 '两个苹果' 属一号).
    倒序黏附覆盖: 物料尾与下一标记间仅隔短运送动词时归该下一标记."""
    if not cms:
        return 0
    cur = cms[0]
    for m in cms:
        if m.start() <= pos:
            cur = m
        else:
            break
    base = cur
    for m in cms:
        if m.start() > mat_end and _is_reverse_attach(clean, mat_end, m.start()):
            base = m
            break
    return YARD_NUM[base.group(1)]


def normalize(text):
    """确定性: 简繁/同音 -> 否定剥离 -> 物料+数量 全文抽取 -> 最近料场归属.
    返回 (clean_text, mentions, negated). 无料场 -> 空提及 (信息不足, 上层暂停);
    命中超纲料场 (4号等) -> 单条 yard=0 提及, 由校验器给出明确拒绝理由."""
    text = text.translate(T2S)
    for a, b in HOMOPHONE:
        text = text.replace(a, b)
    matches = list(YARD_RE.finditer(text))
    if not matches:
        if OOB_YARD_RE.search(text):
            return text, [{"yard": 0, "material": None, "qty": 1}], []
        return text, [], []
    negated = set()
    clean = text
    for name in ("香蕉", "苹果", "梨"):
        pat = re.compile(rf"(?:不要|别要?|不需要|不用)(?:(?!苹果|香蕉|梨).){{0,3}}{name}|{name}(?:不要|不用)")
        if pat.search(clean):
            negated.add(_fruit_action("红苹果" if name == "苹果" else name))
            clean = pat.sub(" ", clean)
    for m in BLOCK_NEG_RE.finditer(clean):
        c = _color_of(m.group(1))
        if c:
            negated.add(c + "_block")
    clean = BLOCK_NEG_RE.sub(" ", clean)
    # 剥离否定后重新扫超纲料场 (混合订单里混进 4号 也要整体暂停)
    if OOB_YARD_RE.search(clean):
        return text, [{"yard": 0, "material": None, "qty": 1}], sorted(negated)
    # 归属: 正序分域 + 倒序黏附 (见 _yard_at). 标记在 clean 上重查
    # (剥否定改变了位置). 子句切分/全局最近距离已弃用: 无标点密集句下
    # 距离判定会把 '一号…两个苹果二号' 的苹果误归二号.
    cms = list(YARD_RE.finditer(clean))
    mentions = []
    for pos, mend, mm in _materials_in(clean):
        mm["yard"] = _yard_at(clean, cms, pos, mend)
        mentions.append(mm)
    return text, mentions, sorted(negated)


MAT_CRIT = {k: v for k, v in MAT_CN.items()}


def laya_decide(mentions, norm_text=""):
    """One Laya request PER mention: 物料终审 (8 选 1), state 带当次提及锚点.
    料场号不终审 (实测基座模型对 "X号" 接地不稳: 1号->2号 p=0.64), 由规则层
    决定 + 校验器白名单检查. 返回 (dt, answers), answers keyed m{i}."""
    answers, t0 = {}, time.perf_counter()
    for i, m in enumerate(mentions):
        if m.get("material") is None or m.get("yard") not in YARDS:
            continue  # 信息不足, 校验器直接给理由, 不再请求
        mat_cn = MAT_CN[m["material"]]
        state = (f"分拣指令原文: {norm_text}. 本次分拣: {mat_cn} x {m['qty']}"
                 f" -> {YARD2CN[m['yard']]}号料场")
        body = json.dumps({"state": state, "model": "jev-latest",
                           "questions": {"q": {"type": "choice",
                                               "instructions": "本次分拣的物料是哪种?",
                                               "criteria": MAT_CRIT}}}).encode()
        req = urllib.request.Request(LAYA, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        answers["m" + str(i)] = d["answers"]["q"]
    return time.perf_counter() - t0, answers


def validate(mentions, answers):
    """确定性白名单 + 交叉检查 + 数量范围; 同料场同物料合并 (PLC 每组一条指令).
    answers=None (LLM 旁路路径): 白名单 + 范围检查."""
    actions, reasons = [], []
    for i, m in enumerate(mentions):
        yard, material = m.get("yard"), m.get("material")
        if yard not in YARDS:
            reasons.append(f"m{i}: 料场越界 {yard!r}")
            continue
        if material is None:
            reasons.append(f"m{i}: 物料不明确")
            continue
        if answers is not None:
            a_m = answers.get("m" + str(i))
            if a_m is None:
                reasons.append(f"m{i}: 缺少物料决策输出")
                continue
            cm = a_m.get("choice")
            if cm not in MATERIALS:
                reasons.append(f"m{i}: 物料不在白名单 {cm!r}")
                continue
            if cm != material:
                reasons.append(f"m{i}: 规则/决策物料冲突 {material} vs {cm}")
                continue
        if not (1 <= m["qty"] <= 99):
            reasons.append(f"m{i}: 数量越界 {m['qty']}")
            continue
        actions.append({"yard": yard, "material": material, "qty": m["qty"]})
    merged = {}
    for a in actions:
        k = (a["yard"], a["material"])
        merged[k] = merged.get(k, 0) + a["qty"]
    final = []
    for (y, mat), qty in merged.items():
        if not (1 <= qty <= 99):
            reasons.append(f"合并后数量越界: 料场{y} {mat} x{qty}")
            continue
        final.append({"yard": y, "material": mat, "qty": qty})
    if not final and not reasons:
        reasons.append("空订单")
    return (not reasons), final, reasons


BYPASS_PROMPT = """你是分拣解析器。把这句话解析成 JSON, 只输出 JSON:
{{"items":[{{"yard":1,"material":"red_apple|green_apple|banana|pear|red_block|green_block|yellow_block|blue_block","qty":1}}]}}
yard 只能是 1/2/3 (对应 1号/2号/3号 料场); 句子没说明是几号料场时 yard 填 0.
material 对应: red_apple=红苹果, green_apple=绿苹果, banana=香蕉, pear=梨,
red_block=红色方块, green_block=绿色方块, yellow_block=黄色方块, blue_block=蓝色方块.
"不要X" 表示 X 不下单, 不列入 items; 数量没说按 1; 没说明颜色的方块不列入.
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
        yard = it.get("yard")
        if not isinstance(yard, int) or yard not in YARDS:
            reasons.append(f"LLM 项 yard={yard!r} 非法 (需 1/2/3)")
            continue
        material = it.get("material", "")
        if material not in MATERIALS:
            reasons.append(f"LLM 项 {material!r} 不在物料白名单")
            continue
        qty = it.get("qty", 1)
        if not isinstance(qty, int) or not (1 <= qty <= 99):
            reasons.append(f"LLM 数量 {qty!r} 非法")
            continue
        mentions.append({"yard": yard, "material": material, "qty": qty,
                         "negated": False})
    # 幻觉防护: LLM 只能下 规则层听到过的 (料场, 物料) 组合
    heard = {(m["yard"], m["material"]) for m in rule_mentions if m.get("material")}
    dropped = [(m["yard"], m["material"]) for m in mentions
               if (m["yard"], m["material"]) not in heard]
    for d in dropped:
        reasons.append(f"LLM 幻觉项(规则层未听到): 料场{d[0]} {d[1]}")
    mentions = [m for m in mentions if (m["yard"], m["material"]) in heard]
    return dt, mentions, reasons


CASES = [
    ("给1号料场送两个红苹果，3号料场送10个蓝色方块",
     [(1, "red_apple", 2), (3, "blue_block", 10)]),   # 双料场双物料
    ("1号料场要5个黄色方块，2号料场来三个香蕉",
     [(1, "yellow_block", 5), (2, "banana", 3)]),
    ("两个红苹果送到3号料场",
     [(3, "red_apple", 2)]),                          # 物料在料场前 (倒序)
    ("1号料场送四个梨，不要绿色方块",
     [(1, "pear", 4)]),                               # 段内否定
    ("3号料场来8个绿色方块和一把香蕉",
     [(3, "green_block", 8), (3, "banana", 1)]),
    # 汉字料场号变体 (ASR/口语常见): 二号 曾漏在 YARD_RE 里被误拒
    ("给二号料场送3个香蕉",
     [(2, "banana", 3)]),
    ("两个红苹果放二号仓库",
     [(2, "red_apple", 2)]),
    ("一号料场送1个梨，二号料场送2个香蕉",
     [(1, "pear", 1), (2, "banana", 2)]),
    # 无标点密集句 (真实语音): 料场分域, 不能按字符距离归属
    ("一号仓库一个香蕉两个苹果二号仓库三个苹果四个香蕉三号仓库一个梨一个香蕉",
     [(1, "banana", 1), (1, "red_apple", 2),
      (2, "red_apple", 3), (2, "banana", 4),
      (3, "pear", 1), (3, "banana", 1)]),
    # 超纲: 应走 暂停+人工 (expected=None 表示只检查路径)
    ("给4号料场送两个苹果", None),                     # 料场越纲
    ("给七号料场送苹果", None),                        # 汉字越纲 (曾致 KeyError)
    ("送两个红苹果", None),                            # 未指定料场
    ("1号料场送3个方块", None),                       # 物料颜色未指定
]


def main():
    print(f"mode={MODE} reps={REPS} endpoint={LAYA}")

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
            got = frozenset((a["yard"], a["material"], a["qty"]) for a in actions)
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
                    exp = "PAUSE" if kind == "oob" else sorted(expected or [])
                    print(f"      expect={exp} got={sorted(got)}  state='{state_text}'")

    n = len(t_e2e)
    print(f"\n===== 料场分拣回路汇总 ({MODE}, {REPS} 轮 x {len(cases)} 句) =====")
    print(f"路径: 快路径 {n_fast}   LLM旁路 {n_bypass}   暂停+人工 {n_pause}   (超纲句计入暂停)")
    print(f"正确: {n_correct}/{n_total}")
    if t_laya:
        print(f"Laya:   mean {statistics.mean(t_laya)*1000:.0f} ms  max {max(t_laya)*1000:.0f} ms  (每提及 1 次请求: 物料终审)")
    if t_bypass:
        print(f"LLM旁路: mean {statistics.mean(t_bypass)*1000:.0f} ms (仅触发时)")
    q = lambda p: sorted(t_e2e)[min(n - 1, int(p * n))] * 1000
    print(f"E2E:    mean {statistics.mean(t_e2e)*1000:.0f} ms  p95 {q(.95):.0f} ms  max {max(t_e2e)*1000:.0f} ms")


if __name__ == "__main__":
    main()
