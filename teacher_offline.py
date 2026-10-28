"""离线教师 (VOICE_DECISION_BENCH.md 设计 ①): LLM 造"语音文本→意图"语料, 交叉标注, 微调 Laya.

LLM (qwen3.5, Ollama, LLM_MODEL env, 默认 qwen3.5:4b) 只做离线侧, 不进实时路径:
**thinking 模式只在教师环节开** (在线旁路 bench_industrial.llm_bypass 保持 think=false,
延迟优先). 实测 4b/2b 的 thinking 不受控 (单句 2000+ token 仍不结束, iGPU ~25 tok/s),
所以教师标注用"thinking 批量摊薄": LLM 生成的新句子按 4 句/批 (num_predict=4096) 开
thinking 标注; 规则模板句 gold 由构造已知, 用无 thinking 快调做一致性抽检.

  1. 生成: 规则模板 + LLM 生成 口语要货句 (冻结 4 动作白名单: 红/绿苹果/香蕉/梨).
     规则层 (bench_industrial.normalize) 做守护: 必须可解析、数量正确、无白名单外词,
     不合格直接丢弃 (与在线旁路同一个白名单思路).
  2. 交叉标注: 每条句子再让 LLM 用要货解析 prompt 独立解析一遍 (带白名单校验 +
     幻觉防护: LLM 只能标规则层听到过的词). 与规则 gold 不一致 → 剔出训练集 +
     记入 QA 报告 (teacher 不一致率是语料质量指标).
  3. 导出 Laya 微调 JSONL ({state, questions, expected}), 用 laya.train.finetune
     微调 base=models/laya-multilingual → out=models/laya-fruits-teacher.
     state 两种形态: 回路原样 "用户要货: 梨 x 3", 和语音转写原文 (单意图句,
     保留 ASR 同音字), 标签都是 4 动作 choice.
  4. 评测: 留出集上 base vs 微调; --cases 复现工业回路 4 个 CASES 的
     Laya 终审 + 校验器, 对比快路径通过率.

thinking 策略 (默认关, --think 开):
  qwen3.5 是思考模型. 实测本机 iGPU + Ollama 上 thinking 不受控: 单句 2000/8192
  token 预算全部烧在 thinking 段、content 恒为空、2b 同样; 提示词限制思考长度无效,
  Ollama 不支持 thinking_budget. 因此离线教师默认 thinking 关 (逐句快调, ~1.2 s/句,
  标签是确定性解析任务, 不依赖长链推理); 在线旁路 bench_industrial 同样 think=false.
  --think 把生成/新句标注切到 thinking 批量 (4 句/批 @4096, 失败批自动降级重试),
  供在思考能正常终止的后端 (如 CUDA GPU / 支持 thinking_budget 的 Ollama) 上开.

用法:
  .venv/bin/python teacher_offline.py generate [--no-llm] [--think]
  .venv/bin/python teacher_offline.py train [--epochs 4]
  .venv/bin/python teacher_offline.py eval [--cases]
  .venv/bin/python teacher_offline.py all

产物 (teacher/): corpus_full.jsonl, train.jsonl, eval.jsonl, qa_report.json;
模型: models/laya-fruits-teacher
"""
import argparse
import json
import os
import random
import statistics
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bench_industrial import ACTIONS, CASES, FRUIT_CRIT, NAME2ACTION, normalize, validate
from bench_pipeline import OLLAMA

MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b")
DATA_DIR = os.path.join(HERE, "teacher")
CORPUS = os.path.join(DATA_DIR, "corpus_full.jsonl")
TRAIN = os.path.join(DATA_DIR, "train.jsonl")
EVAL = os.path.join(DATA_DIR, "eval.jsonl")
QA = os.path.join(DATA_DIR, "qa_report.json")
BASE_CKPT = os.path.join(HERE, "models", "laya-multilingual")
OUT_CKPT = os.path.join(HERE, "models", "laya-fruits-teacher")
DEFAULT_SEED = 20261027

# 名称族: (口语名, 规则层名, 动作). 裸"苹果" 在 NAME2ACTION 里归 red_apple.
FAMILIES = [
    ("苹果", "苹果", "red_apple"),
    ("红苹果", "红苹果", "red_apple"),
    ("绿苹果", "绿苹果", "green_apple"),
    ("香蕉", "香蕉", "banana"),
    ("梨", "梨", "pear"),
]
QTYS = [1, 2, 3, 5, 10]
CN = {1: "一", 2: "两", 3: "三", 5: "五", 10: "十"}

SINGLE_TPL = {
    "苹果": ["给我{q}个苹果", "来{q}个苹果", "我要{q}个苹果", "麻烦给{q}个苹果", "有苹果吗, 来{q}个"],
    "红苹果": ["来{q}个红苹果", "我要{q}个红苹果", "给我{q}个红苹果吧", "红苹果, 来{q}个"],
    "绿苹果": ["我要{q}个绿苹果", "来{q}个绿苹果", "给我{q}个绿苹果谢谢", "绿苹果来{q}个"],
    "香蕉": ["给我{q}根香蕉", "来{q}把香蕉", "我要{q}根香蕉", "香蕉来{q}根", "给我{q}个香蕉"],
    "梨": ["来{q}个梨", "给我{q}个梨", "我要{q}个梨", "麻烦来{q}个梨", "梨来{q}个"],
}
# 同音字/繁体/语气词变体 (模拟 ASR 噪声); 数量词必须在水果名前 (规则层只向前找数量)
NOISY_TPL = {
    "苹果": ["给我{q}個苹果", "我要{q}個苹果"],
    "红苹果": ["来{q}个紅苹果", "我要{q}个紅苹果"],
    "绿苹果": ["给我{q}个绿苹果吧", "来{q}个绿苹果谢谢"],
    "香蕉": ["来{q}根香蕉吧", "给我{q}根香蕉谢谢"],
    "梨": ["给我{q}个离", "来{q}个李", "我要{q}个离吧"],
}

GEN_SINGLE = """你是要货员。用口语写 {n} 条不同的中文要货短句, 每条只点要"{fruit}"这一个水果, 数量正好是{qcn}。
要求: 数量词必须紧跟在水果名前面(例如"三根香蕉", 不能写"香蕉, 三根就行"); 不要出现其他水果; 不要出现"不要"; 可带语气词; 可以偶尔模拟语音转写把字听错(例如"梨"写成"离"或"李", "个"写成"個"), 但每句意思必须清楚。
只输出 JSON 字符串数组, 不要解释: ["句子1", "句子2"]"""

GEN_MULTI = """你是要货员。用口语写 {n} 条不同的中文要货短句, 每条包含且只包含这些水果(数量必须正确): {spec}。
数量词必须紧跟水果名前面; 顺序可以换; 不要出现列表外的水果; 不要出现"不要"; 可带语气词。
只输出 JSON 字符串数组: ["句子1", "句子2"]"""

# 在线旁路 BYPASS_PROMPT 的完整白名单版: 允许 绿苹果 (在线 prompt 只给 苹果|香蕉|梨,
# 离线教师标注需要全 5 名, 否则 "绿苹果" 句永远被误判不一致).
LABEL_PROMPT = """你是要货解析器。把这句话解析成 JSON, 只输出 JSON:
{{"items":[{{"name":"红苹果|绿苹果|苹果|香蕉|梨","qty":1}}]}}
"不要X" 表示 X 不下单, 不列入 items; 数量没说按 1; 数量必须是 1-99 的整数。
句子: {state}"""


def http_post(url, body, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return time.perf_counter() - t0, data


def llm_call(prompt, temperature=0.0, num_predict=300, timeout=180, retries=2, think=False):
    """Ollama generate. Returns (dt, response_text); raises on repeated failure.

    think=True 只用于离线教师 (生成/交叉标注, 质量优先); 在线旁路保持 think=false (延迟优先).
    思考模型的 thinking 段会吃掉 token 预算, think=True 时 num_predict 必须放大."""
    last = None
    for _ in range(retries + 1):
        try:
            dt, d = http_post(OLLAMA, {"model": MODEL, "prompt": prompt, "stream": False,
                                       "think": think,
                                       "options": {"temperature": temperature,
                                                   "num_predict": num_predict, "num_gpu": 99}},
                              timeout)
            return dt, d.get("response", "")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError("ollama call failed: %s" % last)


def last_balanced(txt, closer, opener):
    """最后一个配平的 opener..closer 块. thinking 段可能含不成对的括号, 解析失败时返回 None."""
    end = txt.rfind(closer)
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = txt[i]
            if ch == closer:
                depth += 1
            elif ch == opener:
                depth -= 1
                if depth == 0:
                    return txt[i:end + 1]
        end = txt.rfind(closer, 0, end)
    return None


def parse_json_array(txt):
    block = last_balanced(txt, "]", "[")
    if block is None:
        s, e = txt.find("["), txt.rfind("]")
        block = txt[s:e + 1] if s >= 0 and e > s else None
    if block is None:
        return None
    try:
        arr = json.loads(block)
    except json.JSONDecodeError:
        return None
    return [x for x in arr if isinstance(x, str)] or None


def parse_label(txt):
    block = last_balanced(txt, "}", "{")
    if block is None:
        s, e = txt.find("{"), txt.rfind("}")
        block = txt[s:e + 1] if s >= 0 and e > s else None
    if block is None:
        return None
    try:
        d = json.loads(block)
    except json.JSONDecodeError:
        return None
    return parse_items(d.get("items") if isinstance(d, dict) else None)


def gold_of(utt):
    """规则层 gold: [(action, qty)], 已剥离否定词."""
    _, mentions, _neg = normalize(utt)
    return [(m["action"], m["qty"]) for m in mentions]


def fmt_q(q):
    return CN.get(q, str(q))


def gen_rule_singles(rng):
    rows = []
    for _fruit, name, action in FAMILIES:
        for phr in SINGLE_TPL[name] + NOISY_TPL[name]:
            for q in QTYS:
                rows.append((phr.format(q=fmt_q(q) if rng.random() < 0.7 else str(q)),
                             [(action, q)], "rule"))
    return rows


def gen_rule_multi(rng):
    import itertools
    rows = []
    pairs = list(itertools.combinations(FAMILIES, 2))
    for c in pairs:
        for q1, q2 in [(1, 3), (2, 5)]:
            a = "%s%s" % (fmt_q(q1), c[0][1])
            b = "%s%s" % (fmt_q(q2), c[1][1])
            for phr in ["来%s和%s" % (a, b), "我要%s, 再给%s" % (a, b),
                        "给我%s, 还要%s" % (a, b)]:
                rows.append((phr, [(c[0][2], q1), (c[1][2], q2)], "rule"))
    # 带否定: "我要X, 不要Y"
    for c in itertools.permutations(FAMILIES, 2):
        a = "%s%s" % (fmt_q(1), c[0][1])
        rows.append(("我要%s, 不要%s" % (a, c[1][1]), [(c[0][2], 1)], "rule"))
    return rows


def llm_gen_single(fruit_label, q, n, rng, think=False):
    """LLM 生成单意图句; 规则层验证 (恰好 1 个提及, 动作+数量匹配)."""
    out, dropped = [], []
    for _ in range(3):  # 最多 3 个生成 prompt (同族不同措辞), 凑够 n 条为止
        prompt = GEN_SINGLE.format(n=n, fruit=fruit_label, qcn=fmt_q(q))
        try:
            _dt, txt = llm_call(prompt, temperature=0.7, num_predict=4096, timeout=900, think=think)
        except RuntimeError as e:
            dropped.append(("llm_error", str(e)))
            continue
        cands = parse_json_array(txt) or []
        for u in cands:
            u = u.strip()
            if not u or len(u) > 40 or any(f in u for f in ("不要", "别")):
                dropped.append(("guard", u))
                continue
            g = gold_of(u)
            if g == [(fruit_to_action(fruit_label), q)]:
                out.append((u, [(fruit_to_action(fruit_label), q)], "llm_gen"))
            else:
                dropped.append(("rule_reject", u))
            if len(out) >= n:
                break
        if len(out) >= n:
            break
    return out[:n], dropped


def fruit_to_action(label):
    for _l, name, action in FAMILIES:
        if name == label:
            return action
    raise KeyError(label)


def llm_gen_multi(spec, n, rng, think=False):
    """LLM 生成多意图句; 规则层验证 (提及集合+数量完全匹配)."""
    out, dropped = [], []
    want = frozenset(spec)
    for _ in range(3):
        prompt = GEN_MULTI.format(n=n, spec=spec)
        try:
            _dt, txt = llm_call(prompt, temperature=0.7, num_predict=4096, timeout=900, think=think)
        except RuntimeError as e:
            dropped.append(("llm_error", str(e)))
            continue
        cands = parse_json_array(txt) or []
        for u in cands:
            u = u.strip()
            if not u or len(u) > 60 or any(f in u for f in ("不要", "别")):
                dropped.append(("guard", u))
                continue
            g = gold_of(u)
            if len(g) == len(want) and frozenset(g) == want:
                out.append((u, sorted(g), "llm_gen"))
            else:
                dropped.append(("rule_reject", u))
            if len(out) >= n:
                break
        if len(out) >= n:
            break
    return out[:n], dropped


def llm_label(utt, think=False):
    """教师交叉标注: LLM 独立解析 → 白名单+幻觉防护后的 (action, qty) 列表或 None."""
    dt, txt = llm_call(LABEL_PROMPT.format(state=utt), temperature=0.0, num_predict=4096 if think else 300,
                       timeout=900 if think else 300, think=think)
    items = parse_label(txt)
    return dt, items  # None = 无法解析


LABEL_BATCH_PROMPT = """你是要货解析器。逐条解析下面每句话, 只输出一个 JSON 数组 (不要其他文字),
每个元素形如 {{"idx": 1, "items": [{{"name":"红苹果|绿苹果|苹果|香蕉|梨","qty":1}}]}}
规则: "不要X" 表示 X 不下单, 不列入 items; 数量没说按 1; 数量必须是 1-99 的整数;
items 里只允许白名单里的名字; idx 与句子编号一致。
句子:
{lines}"""


def llm_label_batch(utts, batch_size=4, think=True):
    """批量交叉标注 (thinking 开): thinking 段在整批上摊薄. 返回 {下标: [(action, qty)] 或 None}.

    实测 4b 思考不受控 (单句 2000+ token 仍不结束, 8192 预算照样吃光不出 JSON, 2b 同样),
    所以 thinking 批整批给足预算: 一次 thinking (~3000 token) + 4 句 JSON (~150 token),
    num_predict=4096. 整批截断/无法解析 → 自动降级重试一次 (无 thinking), 仍失败才剔除."""
    out = {i: None for i in range(len(utts))}
    lats = []
    recovered = 0
    for start in range(0, len(utts), batch_size):
        chunk = utts[start:start + batch_size]
        lines = "\n".join("%d. %s" % (i + 1, u) for i, u in enumerate(chunk))
        prompt = LABEL_BATCH_PROMPT.format(lines=lines)
        dt, txt = llm_call(prompt, temperature=0.0, num_predict=4096, timeout=900, think=think)
        lats.append(dt)
        arr = parse_json_array_obj(txt)
        if arr is None:
            print("  !! 批量标注第 %d-%d 句: thinking 输出无法解析, 降级重试 (无 thinking) ..."
                  % (start + 1, start + len(chunk)))
            dt2, txt2 = llm_call(prompt, temperature=0.0, num_predict=1024, timeout=300, think=False)
            lats.append(dt2)
            arr = parse_json_array_obj(txt2)
            if arr is None:
                print("  !! 批量标注第 %d-%d 句: 降级后仍无法解析, 整批剔除" %
                      (start + 1, start + len(chunk)))
                continue
            recovered += 1
        by_idx = {}
        for j, el in enumerate(arr):
            if not isinstance(el, dict):
                continue
            items = parse_items(el.get("items"))
            idx = el.get("idx")
            pos = idx - 1 if isinstance(idx, int) and 1 <= idx <= len(chunk) else j
            if 0 <= pos < len(chunk) and pos not in by_idx:
                by_idx[pos] = items
        for pos, items in by_idx.items():
            out[start + pos] = items
        if (start // batch_size + 1) % 5 == 0:
            print("  批 %d/%d (本批 %.0f s)" %
                  (start // batch_size + 1, (len(utts) + batch_size - 1) // batch_size, dt))
    return out, lats, recovered


def parse_json_array_obj(txt):
    """最后一个配平的 JSON 数组 (元素可为对象). 解析失败返回 None."""
    block = last_balanced(txt, "]", "[")
    if block is None:
        s, e = txt.find("["), txt.rfind("]")
        block = txt[s:e + 1] if s >= 0 and e > s else None
    if block is None:
        return None
    try:
        arr = json.loads(block)
    except json.JSONDecodeError:
        return None
    return arr if isinstance(arr, list) else None


def parse_items(items_raw):
    """items 列表 → [(action, qty)]; 白名单外/数量非法的项直接丢 (幻觉防护). 全丢 → None."""
    if not isinstance(items_raw, list):
        return None
    out = []
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        name = it.get("name", "")
        qty = it.get("qty", 1)
        if not isinstance(name, str) or name not in NAME2ACTION:
            continue
        if isinstance(qty, bool) or not isinstance(qty, int) or not (1 <= qty <= 99):
            continue
        out.append((NAME2ACTION[name], qty))
    return out or None


def generate(args):
    os.makedirs(DATA_DIR, exist_ok=True)
    rng = random.Random(args.seed)
    rule = gen_rule_singles(rng) + gen_rule_multi(rng)
    print("rule templates: %d sentences" % len(rule))

    seen, corpus, dropped_all = set(), [], []
    for u, g, src in rule:
        if u in seen:
            continue
        seen.add(u)
        corpus.append({"utt": u, "gold": g, "source": src})

    if not args.no_llm:
        print("llm warmup ...")
        _dt, _ = llm_call("回复: 好", num_predict=8)
        # 单意图生成: 每族 3 次调用 x 8 条
        for _l, name, action in FAMILIES:
            for q in rng.sample(QTYS, 3):
                got, dropped = llm_gen_single(name, q, args.per_call, rng, think=args.think)
                dropped_all += [(name, q, d) for d in dropped]
                for u, g, src in got:
                    if u not in seen:
                        seen.add(u)
                        corpus.append({"utt": u, "gold": g, "source": src})
        print("single-intent llm generated: %d accepted" % sum(1 for c in corpus if c["source"] == "llm_gen"))
        # 多意图生成: 5 个规格 x 6 条
        spec_pool: list = []
        s = rng.sample(list(FAMILIES), 2)
        spec_pool.append([(s[0][2], 1), (s[1][2], 3)])
        s = rng.sample(list(FAMILIES), 2)
        spec_pool.append([(s[0][2], 2), (s[1][2], 5)])
        s = rng.sample(list(FAMILIES), 3)
        spec_pool.append([(s[0][2], 1), (s[1][2], 1), (s[2][2], 2)])
        for spec in spec_pool[:args.llm_multi]:
            spec_str = ", ".join("%s x %d" % (a, q) for a, q in sorted(spec))
            got, dropped = llm_gen_multi(spec, args.per_multi, rng, think=args.think)
            dropped_all += [(spec_str, None, d) for d in dropped]
            for u, g, src in got:
                if u not in seen:
                    seen.add(u)
                    corpus.append({"utt": u, "gold": g, "source": src})
        print("multi-intent llm generated: %d accepted" %
              sum(1 for c in corpus if c["source"] == "llm_gen" and len(c["gold"]) > 1))

    # 交叉标注: LLM 独立解析, 与规则 gold 对账 (--no-llm 时跳过, 规则 gold 为唯一来源)
    agree = mismatch = unparsed = 0
    llm_items_total = 0
    llm_lat = []
    plain_lat = []
    mismatches = []
    novel_idx = []
    templ_idx = []
    batch_recovered = 0
    if not args.no_llm:
        novel_idx = [i for i, c in enumerate(corpus) if c["source"] == "llm_gen"]
        templ_idx = [i for i, c in enumerate(corpus) if c["source"] != "llm_gen"]
        labeled = {}
        per_item_ms = {}
        batch_recovered = 0
        # 新句子 (LLM 生成): --think 时批量 thinking 标注 (自动降级兜底); 否则逐句快调
        if novel_idx and args.think:
            print("llm 批量交叉标注 (thinking): %d 句新句, 每 %d 句一次调用 ..."
                  % (len(novel_idx), args.label_batch))
            labs, llm_lat, batch_recovered = llm_label_batch(
                [corpus[i]["utt"] for i in novel_idx],
                batch_size=args.label_batch, think=True)
            labeled.update({novel_idx[k]: v for k, v in labs.items()})
            for k, ci in enumerate(novel_idx):
                per_item_ms[ci] = round(llm_lat[min(k // args.label_batch,
                                                    len(llm_lat) - 1)] * 1000) \
                    if llm_lat else None
        # 无 thinking: 其余全部逐句快调 (规则模板句 gold 由构造已知, LLM 只做一致性抽检)
        plain_idx = ([i for i, c in enumerate(corpus)] if not (args.think and novel_idx)
                     else [i for i in novel_idx if labeled.get(i) is None] + templ_idx)
        if not args.think:
            plain_idx = list(range(len(corpus)))
        if plain_idx:
            print("交叉标注 (无 thinking): %d 句, 逐句调用 ..." % len(plain_idx))
            for n, ci in enumerate(plain_idx):
                if labeled.get(ci) is not None:
                    continue
                dt, items = llm_label(corpus[ci]["utt"])  # think=False: 最后兜底
                plain_lat.append(dt)
                labeled[ci] = items
                per_item_ms[ci] = round(dt * 1000)
                if (n + 1) % 50 == 0:
                    print("  plain %d/%d" % (n + 1, len(plain_idx)))
        for i, c in enumerate(corpus):
            items = labeled.get(i)
            c["llm_ms"] = per_item_ms.get(i)
            if items is None:
                c["llm_agree"] = False
                c["llm_items"] = None
                unparsed += 1
                mismatches.append({"utt": c["utt"], "gold": c["gold"], "llm": "unparseable"})
            else:
                llm_items_total += len(items)
                gold_set = frozenset(c["gold"])
                got_set = frozenset(items)
                # 幻觉防护: LLM 只能标规则层听到过的 (action, qty) 组合
                c["llm_agree"] = (got_set == gold_set and len(items) == len(c["gold"]))
                c["llm_items"] = [list(x) for x in items]
                if not c["llm_agree"]:
                    mismatch += 1
                    mismatches.append({"utt": c["utt"], "gold": c["gold"],
                                       "llm": [list(x) for x in items]})
                else:
                    agree += 1
    else:
        for c in corpus:
            c["llm_agree"] = True

    # 剔出不一致句, 拆分 (按句子切, 不跨 split)
    kept = [c for c in corpus if c.get("llm_agree")]
    n_single = sum(1 for c in kept if len(c["gold"]) == 1)
    by_action = {}
    for c in kept:
        a = c["gold"][0][0]
        by_action.setdefault(a, []).append(c)
    train, evl = [], []
    for a, lst in by_action.items():
        lst = sorted(lst, key=lambda c: rng.random())
        k = max(1, int(len(lst) * 0.8))
        train += lst[:k]
        evl += lst[k:]
    rng.shuffle(train)
    rng.shuffle(evl)

    # 导出 Laya JSONL: 单意图句 → 2 行 (回路原样 + 语音转写原文), 多意图 → 每提及 1 行
    def expanded(c):
        out = []
        for i, (action, q) in enumerate(c["gold"]):
            state = "用户要货: %s x %d" % (action_name(action), q)
            out.append((state, action))
            if len(c["gold"]) == 1:
                out.append(("语音转写: " + c["utt"], action))
        return out

    def write_laya(rows, path):
        n = 0
        with open(path, "w", encoding="utf-8") as f:
            for c in rows:
                for s, action in expanded(c):
                    f.write(json.dumps({
                        "state": s,
                        "questions": {"fruit": {"type": "choice",
                                                "instructions": "用户要哪种水果?",
                                                "criteria": FRUIT_CRIT}},
                        "expected": {"fruit": action},
                    }, ensure_ascii=False) + "\n")
                    n += 1
        return n

    n_train = write_laya(train, TRAIN)
    n_eval = write_laya(evl, EVAL)
    with open(CORPUS, "w", encoding="utf-8") as f:
        for c in corpus:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    qa = {
        "model": MODEL,
        "seed": args.seed,
        "think": args.think,
        "sentences_total": len(corpus),
        "sentences_kept": len(kept),
        "single_intent_kept": n_single,
        "multi_intent_kept": len(kept) - n_single,
        "llm_label_batches": len(llm_lat),
        "llm_label_batch_size": args.label_batch,
        "llm_label_batches_recovered": batch_recovered,
        "llm_thinking_sentences": len(novel_idx) if args.think else 0,
        "llm_plain_sentences": len(plain_lat),
        "llm_thinking_mean_ms": round(statistics.mean(llm_lat) * 1000) if llm_lat else None,
        "llm_plain_mean_ms": round(statistics.mean(plain_lat) * 1000) if plain_lat else None,
        "llm_agree": agree,
        "llm_mismatch": mismatch,
        "llm_unparseable": unparsed,
        "agreement_rate": round(agree / max(1, agree + mismatch + unparsed), 4),
        "train_rows": n_train,
        "eval_rows": n_eval,
        "by_action": {a: len(v) for a, v in by_action.items()},
        "dropped_gen_samples": dropped_all[:40],
        "mismatches": mismatches[:40],
    }
    with open(QA, "w", encoding="utf-8") as f:
        json.dump(qa, f, ensure_ascii=False, indent=2)
    print("\n===== 离线教师 generate 汇总 =====")
    print("语料: %d 句 (保留 %d, 单意图 %d / 多意图 %d)" %
          (len(corpus), len(kept), n_single, len(kept) - n_single))
    if args.no_llm:
        print("LLM 交叉标注: 跳过 (--no-llm, 规则 gold 为唯一来源)")
    else:
        print("LLM 交叉标注: agree %d / mismatch %d / unparseable %d  (一致率 %.1f%%)"
              % (agree, mismatch, unparsed, 100 * qa["agreement_rate"]))
        if args.think:
            print("  thinking 批量标注: %d 句新句, %d 批 x %d 句, 批均 %s ms"
                  " (降级恢复 %d 批)" %
                  (qa["llm_thinking_sentences"], qa["llm_label_batches"],
                   qa["llm_label_batch_size"], qa["llm_thinking_mean_ms"],
                   qa["llm_label_batches_recovered"]))
        print("  无 thinking 标注: %d 句, 句均 %s ms"
              % (qa["llm_plain_sentences"] if not args.think else len(plain_lat),
                 qa["llm_plain_mean_ms"]))
    print("训练行: %d  (%s)   评测行: %d  (%s)" % (n_train, TRAIN, n_eval, EVAL))
    print("QA 报告: %s" % QA)
    return 0


def action_name(action):
    """动作 → 回路 state 里的口语名 (red_apple 用裸"苹果", 与 NAME2ACTION 一致)."""
    for _l, name, act in FAMILIES:
        if act == action:
            return "苹果" if name == "苹果" else name
    raise KeyError(action)


def train(args):
    from laya.train import TrainConfig, finetune
    cfg = TrainConfig(epochs=args.epochs, micro_batch=8, grad_accum=4, loss="soft-ce",
                      seed=args.seed, eval_data=EVAL)
    print("base: %s\nout:  %s\ntrain: %s  eval: %s" % (args.base, args.out, TRAIN, EVAL))
    t0 = time.perf_counter()
    summary = finetune(TRAIN, args.base, args.out, cfg, device="cpu")
    print("train done in %.0f s" % (time.perf_counter() - t0))
    rep = summary.get("train_report") or {}
    before = rep.get("before") or {}
    after = rep.get("after") or {}
    keep = {
        "output_dir": summary.get("output_dir"),
        "train_items": summary.get("train_items"),
        "calibration_items": summary.get("calibration_items"),
        "eval_items": summary.get("eval_items"),
        "temperature": summary.get("temperature"),
        "eval_mode": rep.get("eval_mode"),
        "note": rep.get("note"),
        "eval_before": {k: before.get(k) for k in ("accuracy", "loss", "ece", "mean_confidence")},
        "eval_after": {k: after.get(k) for k in ("accuracy", "loss", "ece", "mean_confidence")},
    }
    print(json.dumps(keep, ensure_ascii=False, indent=2, default=str))
    print("checkpoint: %s" % args.out)
    return 0


def load_agent(path):
    import laya
    agent = laya.load(path, device="cpu")
    for _ in range(3):  # warmup
        agent.predict("你好", {"fruit": {"type": "choice", "instructions": "用户要哪种水果?",
                                        "criteria": FRUIT_CRIT}})
    return agent


def eval_split(args):
    with open(EVAL, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    results = {}
    for tag, ckpt in (("base", args.base), ("fine-tuned", args.out)):
        if not os.path.exists(ckpt):
            print("%s: 跳过 (%s 不存在)" % (tag, ckpt))
            continue
        agent = load_agent(ckpt)
        ok = 0
        lat = []
        per = {}
        for r in rows:
            t0 = time.perf_counter()
            res = agent.predict(r["state"], r["questions"])
            lat.append(time.perf_counter() - t0)
            got = res["answers"]["fruit"]["choice"]
            exp = r["expected"]["fruit"]
            ok += got == exp
            d = per.setdefault(exp, [0, 0])
            d[1] += 1
            d[0] += got == exp
        results[tag] = {"n": len(rows), "correct": ok,
                        "acc": round(ok / len(rows), 4),
                        "mean_ms": round(statistics.mean(lat) * 1000),
                        "max_ms": round(max(lat) * 1000),
                        "per_action": {k: "%d/%d" % (c, t) for k, (c, t) in per.items()}}
        print("%s: acc %d/%d = %.1f%%  mean %d ms  max %d ms" %
              (tag, ok, len(rows), 100 * ok / len(rows),
               results[tag]["mean_ms"], results[tag]["max_ms"]))
        for k, v in results[tag]["per_action"].items():
            print("   %-12s %s" % (k, v))
    with open(os.path.join(DATA_DIR, "eval_report.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    if "base" in results and "fine-tuned" in results:
        d = results["fine-tuned"]["acc"] - results["base"]["acc"]
        print("\n微调提升: %+.1f pp (base %.1f%% → fine-tuned %.1f%%)" %
              (100 * d, 100 * results["base"]["acc"], 100 * results["fine-tuned"]["acc"]))
    return 0


def run_cases(args):
    """复现工业回路 4 个 CASES: 规则归一化 → Laya 终审 (每提及单独请求) → 校验器."""
    for tag, ckpt in (("base", args.base), ("fine-tuned", args.out)):
        if not os.path.exists(ckpt):
            continue
        agent = load_agent(ckpt)
        n_fast = n_ok = 0
        for text, expected in CASES:
            _norm, mentions, _neg = normalize(text)
            answers = {}
            for i, m in enumerate(mentions):
                r = agent.predict("用户要货: %s x %d" % (m["name"], m["qty"]),
                                  {"fruit": {"type": "choice", "instructions": "用户要哪种水果?",
                                             "criteria": FRUIT_CRIT}})
                answers["m%d" % i] = {"choice": r["answers"]["fruit"]["choice"]}
            ok, actions, reasons = validate(mentions, answers)
            n_fast += ok
            n_ok += frozenset((a["action"], a["qty"]) for a in actions) == frozenset(expected)
            print("%s  %s  fast=%s  actions=%s%s" %
                  (tag.ljust(11), text, ok, actions,
                   "" if ok else "  reasons=" + "; ".join(reasons)))
        print("%s: 快路径 %d/%d, 结果正确 %d/%d\n" % (tag, n_fast, len(CASES), n_ok, len(CASES)))
    return 0


def main():
    p = argparse.ArgumentParser(description="离线教师: LLM 造语料 + 微调 Laya")
    p.add_argument("cmd", choices=["generate", "train", "eval", "all"])
    p.add_argument("--no-llm", action="store_true", help="generate: 只用规则模板, 不调 Ollama")
    p.add_argument("--per-call", type=int, default=8, help="单意图每次生成的条数")
    p.add_argument("--llm-multi", type=int, default=5, help="多意图生成的规格数")
    p.add_argument("--per-multi", type=int, default=6, help="多意图每次生成的条数")
    p.add_argument("--label-batch", type=int, default=4,
                   help="thinking 批量标注每批句数 (默认 4)")
    p.add_argument("--think", action="store_true",
                   help="生成/新句标注开 thinking (实验性: 本机 iGPU 上 qwen3.5 思考"
                        "不受控, 实测吃光 token 预算且 content 恒为空; 默认关, "
                        "供思考能正常终止的后端使用)")
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--base", default=BASE_CKPT)
    p.add_argument("--out", default=OUT_CKPT)
    p.add_argument("--cases", action="store_true", help="eval: 额外复现工业回路 4 CASES")
    args = p.parse_args()

    if args.cmd in ("generate", "all"):
        generate(args)
    if args.cmd in ("train", "all"):
        train(args)
    if args.cmd in ("eval", "all"):
        eval_split(args)
        if args.cases:
            run_cases(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
