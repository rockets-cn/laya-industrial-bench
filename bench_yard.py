"""料场分拣决策回路基准 (语音/文本 -> 规则层 -> Laya -> 校验器 -> (LLM旁路)).

领域: 料场分拣. 三个料场 (1号/2号/3号), 物料 = 4 种传感器
(角度传感器/超声波传感器/人体红外传感器/温湿度传感器), 口语简称也认.
语音例: "给1号料场送两个角度传感器，3号料场送10个温湿度传感器"
输出:   [{"yard":1,"material":"angle_sensor","qty":2},
         {"yard":3,"material":"temp_humidity_sensor","qty":10}]

架构同 bench_industrial.py / bench_block.py (实时路径不含 LLM):
规则层做 料场号 + 物料 + 数量 抽取 (ms 级, 确定性): 料场号与数量同为白名单
数值, 由规则层决定 + 校验器检查 (实测基座模型对 "X号" 数字接地不稳, 不终审);
Laya 每提及一次前向: 物料终审 (4 选 1, state 带当次提及锚点, 实测 conf≈1.0),
与规则层交叉检查; 校验器做 料场白名单 + 物料白名单 + 数量范围 + 交叉检查,
逐件展开 (1 件 = 1 条 qty=1); LLM (qwen3.5) 仅旁路, 任何失败安全降级为 暂停+人工.

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
import local_qa  # noqa: E402  (本机问答兜底, 纯规则层)

REPS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
MODE = sys.argv[2] if len(sys.argv) > 2 else "text"

# ---- 冻结白名单 (PLC 可执行的 料场集 + 物料集) ----
YARDS = [1, 2, 3]
YARD2CN = {1: "一", 2: "二", 3: "三"}
YARD_NUM = {"1": 1, "2": 2, "3": 3, "一": 1, "二": 2, "两": 2, "俩": 2,
            "三": 3, "幺": 1, "壹": 1}
# 超纲料场 token (4号/四号/11号 ...): 命中即 暂停+人工
OOB_YARD_RE = re.compile(r"(?<!\d)([4-9]\d*|[四五六七八九十])\s*号")

MATERIALS = ["angle_sensor", "ultrasonic_sensor", "pir_sensor", "temp_humidity_sensor"]
MAT_CN = {"angle_sensor": "角度传感器", "ultrasonic_sensor": "超声波传感器",
          "pir_sensor": "人体红外传感器", "temp_humidity_sensor": "温湿度传感器"}
MAT_CN_SHORT = {"angle_sensor": "角度", "ultrasonic_sensor": "超声波",
                "pir_sensor": "红外", "temp_humidity_sensor": "温湿度"}
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 在纲料场: 1号/2号/3号 + 汉字/幺号 (ASR 变体: 二/两/俩), 后接 料场/场地/仓库/库 可选
YARD_RE = re.compile(r"(?<!\d)([123一俩二两三幺壹])\s*号(?:\s*(?:仓库|料场|场地|库|位置|区))?")
# 全料场号 (含超纲 4号/5号/七号...): 用于分域归属 — 超纲号也占有自己的域,
# 其物料由校验器路由到 超纲挂起 (部分下发 PARTIAL), 而不是并入邻近在纲料场
ANY_YARD_RE = re.compile(
    r"(?<!\d)([1-9]\d*|[一二两三四五六七八九十幺壹]+)\s*号(?:\s*(?:仓库|料场|场地|库|位置|区))?")


def _yard_num_of(token):
    """料场号 token -> 数值. 在纲走 YARD_NUM, 超纲用通用数值 (四->4, 七->7, 11->11)."""
    if token in YARD_NUM:
        return YARD_NUM[token]
    if token.isdigit():
        return int(token)
    return _cn_num(token)

# 物料词表 (4 传感器): 全名优先, 口语简称也认 (ASR 噪声); 裸"传感器"= 类型未定
SENSOR_RE = re.compile(
    r"角度传感器|超声波传感器|人体红外传感器|温湿度传感器"
    r"|角度|超声波|人体红外|红外传感器|红外|温湿度|传感器")
SENSOR_KEY = {
    "角度传感器": "angle_sensor", "角度": "angle_sensor",
    "超声波传感器": "ultrasonic_sensor", "超声波": "ultrasonic_sensor",
    "人体红外传感器": "pir_sensor", "人体红外": "pir_sensor",
    "红外传感器": "pir_sensor", "红外": "pir_sensor",
    "温湿度传感器": "temp_humidity_sensor", "温湿度": "temp_humidity_sensor",
    "传感器": None}   # 裸"传感器"未说类型 -> 物料未定 (PARTIAL 挂起报人工)
# 否定提及 (不要/别要/不需要/不用 + 物料词)
SENSOR_NEG_RE = re.compile(
    r"(?:不要|别要?|不需要|不用)\s*"
    r"(角度传感器|超声波传感器|人体红外传感器|温湿度传感器"
    r"|角度|超声波|人体红外|红外传感器|红外|温湿度)")

# 简繁 -> 领域词 (ASR 噪声); 传感器名短, 同音变体已覆盖在 SENSOR_RE 简称词表里
T2S = str.maketrans({"紅": "红", "綠": "绿", "黃": "黄", "藍": "蓝",
                     "塊": "块", "個": "个", "離": "离", "號": "号"})
HOMOPHONE = []  # 同音变体 (a, b) 对; 传感器名短, 暂无需要 (ASR 变体走 SENSOR_RE 简称词表)


def _cn_num(tok):
    """10/十/二十三/一百二十三 -> int. 无法解析返回 1 (规则层保守默认)."""
    if tok.isdigit():
        return int(tok)
    if tok in CN_NUM:
        return CN_NUM[tok]
    if "百" in tok:
        pre, _, rest = tok.partition("百")
        hundreds = (CN_NUM.get(pre, 1) * 100) if pre else 100
        return hundreds + _cn_num(rest) if rest else hundreds
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


_QTY_AFTER = re.compile(
    r"\s*(?:放?(?:到|入|进))?\s*"
    r"(?:[1-9一二两三四五六七八九十幺壹]+号\s*(?:仓库|料场|场地|库|位置|区)?\s*)?"
    r"([一两二三四五六七八九十\d]+)\s*(?:个|块|条)?\s*(?=[，、。！!?,;；\s]|$)")


def _qty_after(text, end):
    """后置数量: "角度传感器三个" (ASR 语序), 以及 料场号夹在中间的后置
    "把角度传感器放到一号仓库两个" / "红外传感器放进2号库三个". 只在边界处认, 避免吞下一项的数量."""
    m = _QTY_AFTER.match(text[end:])
    return _cn_num(m.group(1)) if m else None


def _materials_in(seg):
    """全文 (否定剥离后): 传感器 提及+数量, 按位置排序. 返回 [(pos, end, mention)]
    (裸"传感器" 类型未定 -> material=None, 校验器路由 物料未定 挂起)."""
    found = []
    for m in SENSOR_RE.finditer(seg):
        qty = _qty_before(seg, m.start())
        if qty == 1:
            q2 = _qty_after(seg, m.end())
            if q2:
                qty = q2
        found.append((m.start(), m.end(),
                      {"material": SENSOR_KEY[m.group(0)], "qty": qty}))
    found.sort(key=lambda x: x[0])
    return found


def _nearest_yard(matches, pos):
    """提及归属最近的料场标记 (距离到标记 span); 等距时归后一个料场.
    现在仅作兜底: 子句内多标记 / 子句无标记 时使用 (主归属见 normalize 的
    子句切分). '两个角度传感器送到3号，五个红外给1号' 类倒序句式由子句切分处理."""
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
    ('两个角度传感器放到二号' / '红外给1号' 倒序黏附信号). 空间隔/纯标点 = 正序."""
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
    ('一号仓库一个角度传感器两个超声波二号仓库…' 中 '两个超声波' 属一号).
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
    return _yard_num_of(base.group(1))


def normalize(text):
    """确定性: 简繁/同音 -> 否定剥离 -> 物料+数量 全文抽取 -> 正序分域归属.
    返回 (clean_text, mentions, negated). 无料场 -> 空提及 (信息不足, 上层暂停);
    超纲料场 (4号/5号等) 照常参与分域归属, 其物料由校验器路由到 超纲挂起
    (部分下发 PARTIAL), 在纲项不被阻塞."""
    text = text.translate(T2S)
    for a, b in HOMOPHONE:
        text = text.replace(a, b)
    dist = expand_distribution(text)
    if dist:
        dm, _total, _yards = dist
        return text, dm, []
    # 分配句但目标含超纲料场: 数量是除出来的, 少一个目标在纲项就分多了 ->
    # 整条不下发 (哨兵提及, 校验器拒发 -> 暂停+人工)
    if not dist and DIST_KW_RE.search(text) and OOB_YARD_RE.search(text):
        oob_tokens = sorted({m.group(1) + "号" for m in OOB_YARD_RE.finditer(text)})
        return text, [{"yard": 0, "material": None, "qty": 1,
                        "oob_msg": f"分配目标含超纲料场 {', '.join(oob_tokens)}, 整条指令不下发 (需人工确认)"}], []
    matches = list(ANY_YARD_RE.finditer(text))
    if not matches:
        return text, [], []
    negated = set()
    clean = text
    # 否定剥离: 长名优先 (人体红外传感器 先于 人体红外/红外; 角度传感器 先于 角度)
    for name in ("角度传感器", "超声波传感器", "人体红外传感器", "温湿度传感器",
                 "人体红外", "红外传感器", "红外", "角度", "超声波", "温湿度"):
        pat = re.compile(
            rf"(?:不要|别要?|不需要|不用)(?:(?!角度|超声波|红外|温湿度).){{0,3}}{name}"
            rf"|{name}(?:不要|不用)")
        if pat.search(clean):
            negated.add(SENSOR_KEY[name])
            clean = pat.sub(" ", clean)
    # 归属: 正序分域 + 倒序黏附 (见 _yard_at). 标记在 clean 上重查
    # (剥否定改变了位置). 子句切分/全局最近距离已弃用: 无标点密集句下
    # 距离判定会把 '一号…两个角度传感器二号' 的角度传感器误归二号. 分域用全料场号
    # (含超纲), 保证 '5号放6个红外' 的红外归 5号 而不是并入 2号.
    cms = list(ANY_YARD_RE.finditer(clean))
    mentions = []
    for pos, mend, mm in _materials_in(clean):
        mm["yard"] = _yard_at(clean, cms, pos, mend)
        mentions.append(mm)
    return text, mentions, sorted(negated)


MAT_CRIT = {k: v for k, v in MAT_CN.items()}


def laya_decide(mentions, norm_text=""):
    """One Laya request PER mention: 物料终审 (4 选 1), state 带当次提及锚点.
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
    """确定性白名单 + 交叉检查 + 数量范围; 逐件展开 (1 件 = 1 条 qty=1 动作,
    同料场同物料不合并): 物理上每件 = 一次光门触发 + 一次推杆扫掠 (固件
    Sorter::stepOne 逐件, sim 镜像), 运动时间线也是逐件事件序列, 数字孪生
    按件回放. 超纲料场 (yard 不在 1/2/3) 与 物料未定项 (如传感器未说类型) 不进
    reasons, 路由到 oob 挂起列表 (部分下发): 返回的 reasons 末尾附带 oob 说明 —
    调用方以 ok=True + reasons 非空 识别 PARTIAL.
    answers=None (LLM 旁路路径): 白名单 + 范围检查."""
    actions, reasons, oob = [], [], []
    for i, m in enumerate(mentions):
        yard, material = m.get("yard"), m.get("material")
        if m.get("oob_msg"):
            oob.append(m["oob_msg"])
            continue
        if yard not in YARDS:
            if yard and yard > 0:
                oob.append(f"超纲料场 {yard} 号不下发 ({MAT_CN.get(material, material) if material else '?'} x{m.get('qty', 1)}, 需人工确认)")
            else:
                oob.append(f"料场未指定, 不下发 ({MAT_CN.get(material, material) if material else '?'} x{m.get('qty', 1)}, 需人工确认)")
            continue
        if material is None:
            # 物料未定 (典型: 裸"传感器"未说类型 — 白名单 4 类无法定夺): 该项挂起报人工,
            # 不阻塞其他在纲项 (与 超纲料场 同走 PARTIAL 挂起通道; 纯未定单仍因
            # "空订单" ok=False => 暂停, 行为不变)
            oob.append(f"{yard}号 物料未定, 不下发 (x{m.get('qty', 1)}, 需人工确认种类)")
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
    # 逐件展开: 1 件 = 1 条 qty=1 动作 (同料场同物料不合并 — 每件都是独立的
    # 光门触发+拨料动作, 与固件/sim 逐件执行口径一致; 总量不变, 对账仍成立).
    final = []
    for a in actions:
        for _ in range(a["qty"]):
            final.append({"yard": a["yard"], "material": a["material"], "qty": 1})
    if not final and not reasons:
        reasons.append("空订单")
    # 分配指令对账: 展开后的总量必须与指令说的总数精确一致 (容差 0)
    dist_total = next((m.get("dist_total") for m in mentions
                       if m.get("dist_total")), None)
    if dist_total and final:
        s = sum(a["qty"] for a in final)
        if s != dist_total:
            reasons.append(f"分配对账失败: 展开总量 {s} != 指令 {dist_total}")
    # oob 挂起说明附在返回 reasons 末尾: ok=True+reasons 非空 => 部分下发 (PARTIAL);
    # 纯超纲单 (无在纲项) 因 "空订单" 进 reasons 仍 ok=False => 旁路/暂停, 行为不变
    return (not reasons), final, reasons + oob


BYPASS_PROMPT = """你是分拣解析器。把这句话解析成 JSON, 只输出 JSON:
{{"items":[{{"yard":1,"material":"angle_sensor|ultrasonic_sensor|pir_sensor|temp_humidity_sensor","qty":1}}]}}
yard 只能是 1/2/3 (对应 1号/2号/3号 料场); 句子没说明是几号料场时 yard 填 0.
material 对应: angle_sensor=角度传感器, ultrasonic_sensor=超声波传感器,
pir_sensor=人体红外传感器, temp_humidity_sensor=温湿度传感器.
"不要X" 表示 X 不下单, 不列入 items; 数量没说按 1; 没说明类型的传感器不列入.
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


# ---- 分配指令 ("99个角度传感器平均分到三个仓库") ----
# 第一层: 确定性展开 (毫秒级, 无 LLM): 平均/平分 N 个 X 到 M 个仓库, 余数给小号
# 第二层: 规则层拿不到完整结构的分配句 -> LLM 展开 + 校验器总量对账 (路径 EXPAND)
DIST_KW_RE = re.compile(r"平均|平分|均分|均匀分")
# 总量+物料: "九十九个角度传感器" / "100个超声波传感器" / "十个红外"
DIST_TOT_RE = re.compile(
    r"([0-9]+|[一二两三四五六七八九十百]+)\s*(?:个|把|条|块|根|只|颗)?\s*"
    r"(角度传感器|超声波传感器|人体红外传感器|温湿度传感器"
    r"|角度|超声波|人体红外|红外|温湿度)")
# 目标 "M 个仓库/料场" (M=1..3 -> 前 M 个料场)
DIST_M_RE = re.compile(r"([一二两三123])\s*(?:个|座|间)?\s*(?:仓库|料场|场地)")
# 点名 "1号和3号" / "一号 与 三号"
DIST_EXPL_RE = re.compile(r"([123一二两])\s*号\s*(?:和|、|与|,|，)\s*([123一二两])\s*号")


def _dist_material(raw):
    m = re.search(SENSOR_RE, raw)
    return SENSOR_KEY[m.group(0)] if m else None


def expand_distribution(text):
    """Tier-1 确定性分配展开. 返回 ([mentions], total, yards) 或 None
    (结构不全 -> 上层可走 LLM 展开). 每个 mention 带 dist_total 供校验器对账.
    分配目标含超纲料场 (4号/5号等): 拒绝展开 — 数量是除出来的, 目标集合缺一个
    每个在纲料场就分多了, 必须整条暂停 (由 normalize 转 哨兵提及)."""
    if not DIST_KW_RE.search(text):
        return None
    if OOB_YARD_RE.search(text):
        return None
    m = DIST_TOT_RE.search(text)
    if not m:
        return None
    total = _cn_num(m.group(1))
    material = _dist_material(m.group(2))
    if not material or not (1 <= total <= 99 * len(YARDS)):
        return None
    exp = DIST_EXPL_RE.search(text)
    if exp:
        a, b = YARD_NUM[exp.group(1)], YARD_NUM[exp.group(2)]
        yards = [a, b]
    else:
        mm = DIST_M_RE.search(text)
        if mm and _cn_num(mm.group(1)) in (1, 2, 3):
            yards = list(range(1, _cn_num(mm.group(1)) + 1))
        elif re.search(r"所有|全部", text):
            yards = list(YARDS)
        else:
            return None  # 目标不明 -> LLM 层
    base, r = divmod(total, len(yards))
    mentions = []
    for i, y in enumerate(sorted(set(yards))):
        q = base + (1 if i < r else 0)  # 余数给小号料场
        if q >= 1:
            mentions.append({"material": material, "qty": q, "yard": y})
    if not mentions:
        return None
    for mm in mentions:
        mm["dist_total"] = total
    return mentions, total, yards


DIST_EXPAND_PROMPT = """你是分拣解析器。把这条"分配"指令展开成每个料场的 JSON, 只输出 JSON:
{{"items":[{{"yard":1,"material":"angle_sensor","qty":33}}]}}
规则:
- yard 只能是 1/2/3 (对应 1号/2号/3号 料场); 指令没指明具体哪几个料场时, 按 1/2/3 号全部料场处理;
- material 只能从这些里选: angle_sensor=角度传感器, ultrasonic_sensor=超声波传感器,
  pir_sensor=人体红外传感器, temp_humidity_sensor=温湿度传感器;
  口语简称 (角度/超声波/红外/温湿度) 对应上述 key; 没说明类型的"传感器"不列入;
- 指令说把 N 件物料平均/分给若干料场时: **你的职责是判断料场集合**, qty 只写一个参考值
  (规则层会按"余数给小号料场"重算数量); 各项 qty 之和必须等于 N, 分到 0 的料场不列入;
- 分配对象确实无法确定时 yard 填 0 (会被拒绝并转人工).
指令: {state}"""


def llm_dist_expand(text):
    """Tier-2: LLM 把模糊分配句展开成 JSON. 返回 (dt, mentions, reasons)."""
    dt, parsed = llm_json(DIST_EXPAND_PROMPT.format(state=text))
    if parsed is None or not isinstance(parsed, dict):
        return dt, None, ["LLM 分配输出无法解析"]
    items = parsed.get("items", [])
    if not isinstance(items, list):
        return dt, None, ["LLM 分配输出无法解析"]
    mentions, reasons = [], []
    for it in items:
        yard = it.get("yard")
        if not isinstance(yard, int) or yard not in YARDS:
            reasons.append(f"LLM 分配项 yard={yard!r} 非法 (需 1/2/3)")
            continue
        material = it.get("material", "")
        if material not in MATERIALS:
            reasons.append(f"LLM 分配项 {material!r} 不在物料白名单")
            continue
        qty = it.get("qty", 1)
        if not isinstance(qty, int) or not (1 <= qty <= 99):
            reasons.append(f"LLM 分配数量 {qty!r} 非法")
            continue
        mentions.append({"yard": yard, "material": material, "qty": qty,
                         "dist_total": 0})
    return dt, mentions, reasons


def dist_expand_if_needed(text):
    """规则层空结果 + 分配指令可识别为分配句 -> LLM 展开 (指令). 返回 (mentions|None, dt, reasons).
    防幻觉: 物料必须与规则层在句子里听到的物料一致; 总量由规则层给出, 校验器对账."""
    if not DIST_KW_RE.search(text):
        return None
    if OOB_YARD_RE.search(text):
        return None  # 分配目标含超纲料场: 不展开, 整条暂停
    m = DIST_TOT_RE.search(text)
    if not m:
        return None
    raw, total = m.group(2), _cn_num(m.group(1))
    material = _dist_material(raw)
    if not material or not (1 <= total <= 99 * len(YARDS)):
        return None
    dt, lmentions, reasons = llm_dist_expand(text)
    if not lmentions:
        return None, dt, reasons or ["LLM 无输出"]
    bad = [x for x in lmentions if x["material"] != material]
    reasons += [f"LLM 分配物料与指令不符 ({x['material']} vs {material})" for x in bad]
    lmentions = [x for x in lmentions if x["material"] == material]
    if not lmentions:
        return None, dt, reasons
    # 均分数量由规则层裁定 (余数给小号料场); LLM 只决定料场集合.
    # LLM 的 qty 仅用于上面的范围校验, 不信任其分配 (实测 qwen3.5-4b 会把多的 1 件给大号).
    yard_set = sorted({x["yard"] for x in lmentions})
    base, r = divmod(total, len(yard_set))
    lmentions = []
    for i, y in enumerate(yard_set):
        q = base + (1 if i < r else 0)  # 余数给小号料场
        if q >= 1:
            lmentions.append({"yard": y, "material": material, "qty": q})
    if not lmentions:
        return None, dt, reasons + ["展开为空"]
    for x in lmentions:
        x["dist_total"] = total  # 对账锚点, 校验器强制 总量 == total
    return lmentions, dt, reasons


CASES = [
    ("给1号料场送两个角度传感器，3号料场送10个温湿度传感器",
     [(1, "angle_sensor", 1), (3, "temp_humidity_sensor", 1)]),   # 双料场双物料 (逐件展开)
    ("1号料场要5个超声波传感器，2号料场来三个人体红外传感器",
     [(1, "ultrasonic_sensor", 1), (2, "pir_sensor", 1)]),
    ("两个角度传感器送到3号料场",
     [(3, "angle_sensor", 1)]),                          # 物料在料场前 (倒序)
    ("1号料场送四个超声波传感器，不要人体红外传感器",
     [(1, "ultrasonic_sensor", 1)]),                               # 段内否定
    ("3号料场来8个温湿度传感器和一个人体红外传感器",
     [(3, "temp_humidity_sensor", 1), (3, "pir_sensor", 1)]),
    # 汉字料场号变体 (ASR/口语常见): 二号 曾漏在 YARD_RE 里被误拒
    ("给二号料场送3个红外传感器",
     [(2, "pir_sensor", 1)]),
    ("两个超声波传感器放二号仓库",
     [(2, "ultrasonic_sensor", 1)]),
    # 后置数量且料场号夹在物料与数量之间 (真实语音语序: 先报物料再说放到哪几件)
    ("把角度传感器放到一号仓库两个",
     [(1, "angle_sensor", 1)]),
    ("一号料场送1个温湿度传感器，二号料场送2个超声波传感器",
     [(1, "temp_humidity_sensor", 1), (2, "ultrasonic_sensor", 1)]),
    # 无标点密集句 (真实语音): 料场分域, 不能按字符距离归属
    ("一号仓库一个角度传感器两个超声波传感器二号仓库三个人体红外传感器四个温湿度传感器三号仓库一个角度传感器一个超声波传感器",
     [(1, "angle_sensor", 1), (1, "ultrasonic_sensor", 1),
      (2, "pir_sensor", 1), (2, "temp_humidity_sensor", 1),
      (3, "angle_sensor", 1), (3, "ultrasonic_sensor", 1)]),
    # 口语 '两号' (ASR 常转成 两号 而非 二号)
    ("一号仓库放两个角度传感器两号仓库放三个人体红外传感器三号仓库放四个超声波传感器",
     [(1, "angle_sensor", 1), (2, "pir_sensor", 1), (3, "ultrasonic_sensor", 1)]),
    # 分配指令: Tier-1 确定性展开 (余数给小号料场); 逐件后每料场 1 条 qty=1
    ("九十九个角度传感器平均分到三个仓库里去",
     [(1, "angle_sensor", 1), (2, "angle_sensor", 1), (3, "angle_sensor", 1)]),
    ("100个超声波传感器平均分到三个仓库",
     [(1, "ultrasonic_sensor", 1), (2, "ultrasonic_sensor", 1),
      (3, "ultrasonic_sensor", 1)]),
    ("把40个红外传感器平均分到1号和3号仓库",
     [(1, "pir_sensor", 1), (3, "pir_sensor", 1)]),
    ("两个温湿度传感器平均分到1号和3号",
     [(1, "temp_humidity_sensor", 1), (3, "temp_humidity_sensor", 1)]),
    ("两个人体红外传感器平均分到三个仓库",
     [(1, "pir_sensor", 1), (2, "pir_sensor", 1)]),
    ("把角度传感器平均分到仓库里", None),                      # 无数量 -> 暂停等人工
    # 超纲: 应走 暂停+人工 (expected=None 表示只检查路径)
    ("给4号料场送两个角度传感器", None),                     # 料场越纲
    ("给七号料场送超声波", None),                        # 汉字越纲 (曾致 KeyError)
    ("送两个人体红外传感器", None),                            # 未指定料场
    ("1号料场送3个传感器", None),                       # 传感器类型未指定 -> 物料未定
    # 超纲混入有效指令: 部分下发 — 在纲项照发, 超纲项挂起报人工 (PARTIAL)
    ("一号料场放三个角度传感器，2号料场放4个超声波传感器，5号料场放6个红外传感器",
     [(1, "angle_sensor", 1), (2, "ultrasonic_sensor", 1)], "partial"),
    # 传感器未说类型: 物料未定项挂起报人工, 其余在纲项照发 (PARTIAL)
    ("一号库径三个传感器，2号库径4个角度传感器，3号库径5个温湿度传感器",
     [(2, "angle_sensor", 1), (3, "temp_humidity_sensor", 1)], "partial"),
    # 本机运行状态问答 (纯规则层, 答案<=30字): expected = 答案必含子串
    ("现在几点了", "现在", "qa"),
    ("明天是星期几", "明天是星期", "qa"),
    ("CPU占用多少", "%", "qa"),
    ("8002还在跑吗", "8002", "qa"),
    # 料场库存 (默认答 累计库存/台账; 显式 最近/上次 才答 最近一次分拣指令; 库空答 暂无库存)
    ("1号料场有多少件", "1号", "qa"),
    ("最近一次1号料场的指令有多少", "1号", "qa"),
    # 累计库存 (SQLite 出入库台账, 分拣下发自动记入; 库空时答 暂无库存)
    ("现在各个仓库分别有多少内容？", "件", "qa"),
    ("现在仓库里有多少东西？", "件", "qa"),
    # 指定料场 累计库存 (台账; 默认答案) + 物料→仓库 反查
    ("现在3号仓库有多少货？", "3号", "qa"),
    ("哪个仓库有人体红外传感器？", "人体红外", "qa"),
    ("角度传感器在哪个仓库里？", "角度", "qa"),
]


def _cases_from():
    """CASES -> (text, expected, kind). kind: scored|oob|qa (三元素组显式标注)."""
    out = []
    for c in CASES:
        kind = c[2] if len(c) > 2 else ("oob" if c[1] is None else "scored")
        out.append((c[0], c[1], kind))
    return out


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
            cases = _cases_from()
    else:
        cases = _cases_from()

    t_laya, t_bypass, t_e2e = [], [], []
    n_fast = n_expand = n_bypass = n_partial = n_pause = n_qa = n_correct = n_total = 0
    local_qa.set_last_order([{"yard": 1, "material": "angle_sensor", "qty": 2},
                              {"yard": 2, "material": "ultrasonic_sensor", "qty": 5}])  # 问答用例基准库存
    for rep in range(REPS):
        for ci, (state_text, expected, kind) in enumerate(cases):
            t0 = time.perf_counter()
            if kind == "qa":
                qhit, qans, _qdt = local_qa.answer(state_text)
                correct = qhit and isinstance(expected, str) and expected in qans
                path = "QA" if qhit else "PAUSE+HUMAN"
                t_e2e.append(time.perf_counter() - t0)
                n_total += 1
                n_correct += correct
                if path == "QA":
                    n_qa += 1
                note = "" if qhit else "  [未识别为问句]"
                print(f"rep{rep} c{ci} [{path:12s}] {'OK ' if correct else 'WRONG'} (问答)"
                      f"{note} answer='{qans}'  期望含'{expected}'")
                continue
            norm, mentions, neg = normalize(state_text)
            dist_ms = 0.0
            if not mentions and not neg:
                dm, dt_d, _dr = dist_expand_if_needed(norm) or (None, 0.0, [])
                if dm:
                    mentions, dist_ms = dm, dt_d
            dt_l, answers = laya_decide(mentions, norm) if mentions else (0.0, {})
            t_laya.append(dt_l)
            ok, actions, reasons = validate(mentions, answers)
            path, dt_b = ("EXPAND" if dist_ms else "FAST"), 0.0
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
            elif reasons:
                # ok=True + 仅 oob 挂起说明: 在纲项照发, 超纲项报人工
                path = "PARTIAL"
            e2e = time.perf_counter() - t0
            t_e2e.append(e2e)
            got = frozenset((a["yard"], a["material"], a["qty"]) for a in actions)
            if kind == "report":
                print(f"rep{rep} c{ci} [{path:12s}] (语音, 仅报告) "
                      f"actions={actions}  neg={neg}  reasons={reasons[:2]}")
            else:
                correct = (path == "PAUSE+HUMAN") if kind == "oob" \
                    else ((path == "PARTIAL" and got == frozenset(expected)) if kind == "partial"
                          else (got == frozenset(expected)))
                n_total += 1
                n_correct += correct
                if path == "FAST":
                    n_fast += 1
                elif path == "EXPAND":
                    n_expand += 1
                elif path == "BYPASS":
                    n_bypass += 1
                elif path == "PARTIAL":
                    n_partial += 1
                else:
                    n_pause += 1
                print(f"rep{rep} c{ci} [{path:12s}] {'OK ' if correct else 'WRONG'} "
                      f"{'(超纲, 期望暂停)' if kind == 'oob' else '(部分下发, 超纲挂起)' if kind == 'partial' else ''} "
                      f"actions={actions}  reasons={reasons[:2]}")
                if not correct:
                    exp = ("PAUSE" if kind == "oob"
                           else ("PARTIAL " + str(sorted(expected or []))) if kind == "partial"
                           else sorted(expected or []))
                    print(f"      expect={exp} got={sorted(got)}  state='{state_text}'")

    n = len(t_e2e)
    print(f"\n===== 料场分拣回路汇总 ({MODE}, {REPS} 轮 x {len(cases)} 句) =====")
    print(f"路径: 快路径 {n_fast}   LLM分配 {n_expand}   LLM旁路 {n_bypass}   部分下发 {n_partial}   问答 {n_qa}   暂停+人工 {n_pause}")
    print(f"正确: {n_correct}/{n_total}")
    if t_laya:
        print(f"Laya:   mean {statistics.mean(t_laya)*1000:.0f} ms  max {max(t_laya)*1000:.0f} ms  (每提及 1 次请求: 物料终审)")
    if t_bypass:
        print(f"LLM旁路: mean {statistics.mean(t_bypass)*1000:.0f} ms (仅触发时)")
    q = lambda p: sorted(t_e2e)[min(n - 1, int(p * n))] * 1000
    print(f"E2E:    mean {statistics.mean(t_e2e)*1000:.0f} ms  p95 {q(.95):.0f} ms  max {max(t_e2e)*1000:.0f} ms")


if __name__ == "__main__":
    main()
