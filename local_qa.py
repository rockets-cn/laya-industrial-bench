"""本机运行状态简单问答 (纯规则层, 零 LLM, 毫秒级).

定位: 料场分拣回路收到"问句"而非"分拣指令"时的兜底回答能力.
硬约束:
- 答案 <= 30 字 (超出截断);
- 查询全程有超时预算 (端口探测 0.4s/个, 总预算 3s), 超时答"查询超时";
- 只回答能确定性取得的事实; 识别不出类别 -> 不答 (return hit=False),
  原句继续走分拣路径 (空订单 -> PAUSE+HUMAN), 绝不编造.

类别:
1. 时间/日期   "现在几点了" / "今天星期几" / "明天是星期几" (相对日期的星期)
2. 料场状态   "1号料场有多少件" (最近一次分拣指令的库存, 由 loop_ui 注入)
3. 服务状态   "8002还在跑吗" / "网关正常吗" (本机 TCP 端口探测)
4. 资源占用   "CPU占用多少" / "内存和磁盘" (/proc/loadavg, /proc/meminfo, disk_usage)
5. LLM 兜底   (可选, 默认关) 规则层认不出的问句, 让 LLM 基于注入事实回答;
              开启方式: 环境变量 QA_LLM=1 (见下方 LLM 兜底层 一节)
"""
import datetime
import json
import os
import re
import shutil
import socket
import time
import urllib.request

MAX_ANSWER_LEN = 30      # 答案硬上限 (字)
QUERY_BUDGET_S = 3.0    # 整个问答的耗时预算
PORT_PROBE_S = 0.4      # 单端口 TCP 探测超时

# 分拣物料词: 句子里出现物料名 -> 视为分拣指令, 不进问答 (防误抢)
_MATER_RE = re.compile(r"香蕉|苹果|梨|方块")
# 料场号
_YARD_Q_RE = re.compile(r"([123一二两])\s*号\s*(?:料场|仓库|场地|库|位置|区)?")
_YARD_NUM = {"1": 1, "2": 2, "3": 3, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3}
_DAY_CN = "一二三四五六日"   # 星期映射: 下标 0=周一 ... 6=周日 (对齐 tm_wday/weekday())

# 关键词 -> 端口 (服务状态问句)
_PORT_KW = [
    (re.compile(r"8002|网页|控制台|UI|ui|loop|界面"), 8002, "控制台"),
    (re.compile(r"8000|laya|Laya|LAYA|推理"), 8000, "Laya"),
    (re.compile(r"8123|网关|dispatch|分拣器|MQTT|mqtt"), 8123, "网关"),
    (re.compile(r"11434|ollama|Ollama|模型服务"), 11434, "Ollama"),
]
_TIME_KW = re.compile(r"几点|时间|星期|日期|几月|今天|现在.*点")
_RES_KW = re.compile(r"CPU|cpu|占用|内存|磁盘|硬盘|空间|负载|用了多少")
_QA_KW = re.compile(
    r"几点|时间|星期|周几|礼拜几|日期|还在|是否|正常吗|正常嘛|占用|多少|多长|几件|加载|"
    r"内存|硬盘|磁盘|网络|CPU|cpu|服务|进程|端口|回答|是什么|状态|还有|跑吗|在吗")

# 最近一次分拣指令 (由 loop_ui 在每次 /run 后注入)
_LAST_ORDER = []


def set_last_order(actions):
    global _LAST_ORDER
    _LAST_ORDER = [a for a in actions if a.get("yard")]


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PORT_PROBE_S):
            return True
    except OSError:
        return False


def _loadavg():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[0]
    except OSError:
        return None


def _mem_pct():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                info[k] = int(rest.strip().split()[0])  # kB
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        if total <= 0:
            return None
        return round((total - avail) * 100 / total)
    except (OSError, ValueError, IndexError):
        return None


def _disk_pct():
    try:
        du = shutil.disk_usage("/")
        return round(du.used * 100 / du.total)
    except OSError:
        return None


def _ans_time():
    now = time.localtime()
    return f"现在{time.strftime('%H:%M', now)} 星期{_DAY_CN[now.tm_wday]}"


# 相对日期 -> 天数偏移 (今天/明天/后天; 大后天须先于 后天 匹配)
_DAY_OFFSET = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}


def _ans_weekday(text):
    """相对日期的星期: "明天是星期几" -> "明天是星期四". 无法确定的 (下周/上周/这周) 不答."""
    if re.search(r"下周|上周|这周|本周末|周初|月底|月初", text):
        return None
    m = re.search(r"大后天|明天|明日|后天|今天|今日", text)
    label, off = (m.group(0), _DAY_OFFSET[m.group(0)]) if m else ("今天", 0)
    d = datetime.date.today() + datetime.timedelta(days=off)
    return f"{label}是星期{_DAY_CN[d.weekday()]}"


def _ans_yard(text):
    m = _YARD_Q_RE.search(text)
    if not m:
        return None
    y = _YARD_NUM[m.group(1)]
    if not _LAST_ORDER:
        return f"{y}号料场: 还没有分拣记录"
    items = [a for a in _LAST_ORDER if a.get("yard") == y]
    tot = sum(a.get("qty", 0) for a in items)
    if not items:
        return f"{y}号料场: 0件"
    detail = " ".join(
        f"{_MATCN[a['material']]}x{a['qty']}"
        for a in items if a.get("material") in _MATCN)
    return f"{y}号料场: {tot}件 " + detail


def _ans_service(text):
    ports = [(p, n) for pat, p, n in _PORT_KW if pat.search(text)]
    if not ports:  # 泛称"服务/进程/端口" -> 全查
        ports = [(8002, "控制台"), (8000, "Laya"), (8123, "网关"), (11434, "Ollama")]
    parts = []
    for p, n in ports:
        parts.append(f"{p}{'运行中' if _port_open(p) else '未运行'}")
    return " ".join(parts)


def _ans_resource(text):
    load, mem, disk = _loadavg(), _mem_pct(), _disk_pct()
    if re.search(r"内存", text) and not re.search(r"CPU|cpu|磁盘|硬盘|负载", text):
        return f"内存占用 {mem}%" if mem is not None else "内存查不到"
    if (re.search(r"磁盘|硬盘|空间", text)
            and not re.search(r"CPU|cpu|内存|负载", text)):
        return f"磁盘占用 {disk}%" if disk is not None else "磁盘查不到"
    seg = []
    if load is not None:
        seg.append(f"负载{load}")
    if mem is not None:
        seg.append(f"内存{mem}%")
    if disk is not None:
        seg.append(f"磁盘{disk}%")
    return " ".join(seg) if seg else "资源查不到"


_MATCN = {"red_apple": "红苹果", "green_apple": "绿苹果", "banana": "香蕉",
          "pear": "梨", "red_block": "红方块", "green_block": "绿方块",
          "yellow_block": "黄方块", "blue_block": "蓝方块"}


# ---------------------------------------------------------------------------
# LLM 兜底层 (可选, 默认关) — 设计说明
#
# 定位: 规则层 (answer(), 毫秒级/零幻觉) 认不出的长尾问句, 让 LLM 基于
#       "注入事实" 回答。规则层仍是主路径; 本层只补覆盖, 不进实时快路径。
# 为什么默认关:
#   1) 本机 Ollama 4b 实测 10~16 s/请求, 远慢于规则层毫秒级 — 只在用户明确
#      提问且规则层 miss 时才值得花这个时间;
#   2) LLM 没有时钟/运行状态感知, 答案只能是注入事实的转述 — 事实缺失必须
#      答"不知道", 绝不编造 (与项目安全纪律一致)。
# 安全纪律 (同分拣旁路):
#   - 答案只展示给用户, QA 路径没有任何 PLC/分拣输出 — LLM 答错也不会执行;
#   - 答案只允许转述注入事实; 超时/失败/空答案/答"不知道" -> hit=False,
#     该句照旧回落分拣路径 (空订单 -> PAUSE+HUMAN);
#   - 物料词 (香蕉/苹果/梨/方块) 一律视为分拣指令, 不进问答 (含 LLM 兜底)。
# 开关: 环境变量 QA_LLM=1 (服务启动时生效, 如 QA_LLM=1 python3 loop_ui.py);
#       模型可用 QA_LLM_MODEL 覆盖 (默认 qwen3.5:4b, 与旁路同款)。
# ---------------------------------------------------------------------------
OLLAMA = "http://127.0.0.1:11434/api/generate"
LLM_MODEL = os.environ.get("QA_LLM_MODEL", "qwen3.5:4b")
LLM_TIMEOUT_S = 20.0          # 4b iGPU 实测 10~16s; 超 20s 直接回落分拣路径
LLM_ENABLED = os.environ.get("QA_LLM") == "1"   # 默认关

LLM_QA_PROMPT = (
    "你是工业机本地问答助手。只能依据下面的【事实】回答用户问题; "
    "问题内容不在事实覆盖范围内就只回答\"不知道\", 禁止编造事实, 禁止执行任何动作。"
    "答案不超过 30 字, 只给结论。\n"
    "【事实】\n{facts}\n问题: {q}\n回答:")


def llm_enabled():
    """LLM 兜底是否开启 (QA_LLM=1)。供 loop_ui 判断是否走兜底分支."""
    return LLM_ENABLED


def _collect_facts():
    """事实清单: 只放本机可确定性取到的事实 — LLM 只能转述这些, 不能创造."""
    now = time.localtime()
    lines = [f"当前时间: {time.strftime('%Y-%m-%d %H:%M', now)}, 星期{_DAY_CN[now.tm_wday]}"]
    for _pat, p, n in _PORT_KW:
        lines.append(f"服务 {n} (端口 {p}): {'运行中' if _port_open(p) else '未运行'}")
    seg = []
    load, mem, disk = _loadavg(), _mem_pct(), _disk_pct()
    if load is not None:
        seg.append(f"负载 {load}")
    if mem is not None:
        seg.append(f"内存 {mem}%")
    if disk is not None:
        seg.append(f"磁盘 {disk}%")
    lines.append("系统: " + (" ".join(seg) if seg else "未知"))
    lines.append("料场编号: 1/2/3 (其他号超纲)")
    lines.append(f"支持物料: {'、'.join(_MATCN.values())}")
    if _LAST_ORDER:
        s = "; ".join(f"{a.get('yard')}号 {_MATCN.get(a.get('material'), '?')} x{a.get('qty')}"
                      for a in _LAST_ORDER if a.get("material") in _MATCN)
        lines.append(f"最近一次分拣指令: {s}" if s else "最近一次分拣指令: 无")
    else:
        lines.append("最近一次分拣指令: 无")
    return "\n".join(lines)


# 兜底层的问句判定: 规则层关键词表 (_QA_KW) 或 中文疑问词 — 比规则层宽,
# 因为本层就是为 规则层认不出的 问句 兜底; 分拣指令仍被物料词门拦在外面。
_INTERROG_RE = re.compile(
    r"吗|呢|什么|怎么|为什么|哪个|哪一|是谁|几[个种次号点日]|多少|有无|能否|可否")


def answer_llm(text):
    """LLM 兜底问答 (仅 loop_ui 在 规则层 miss 且 llm_enabled() 时调用).
    返回 (hit, answer, dt); hit=False -> 调用方照旧回落分拣路径.
    答案 <=30 字 (超出截断); 空/超时/异常/答"不知道" 一律 hit=False."""
    t0 = time.perf_counter()
    t = (text or "").strip()
    # 物料词 = 分拣指令, 绝不进问答 (与规则层同门); 非问句不答
    if not t or _MATER_RE.search(t):
        return False, "", 0.0
    if not (_QA_KW.search(t) or _INTERROG_RE.search(t)):
        return False, "", 0.0
    try:
        prompt = LLM_QA_PROMPT.format(facts=_collect_facts(), q=t)
        body = json.dumps({"model": LLM_MODEL, "prompt": prompt, "stream": False,
                            "think": False,
                            "options": {"temperature": 0, "num_predict": 120,
                                         "num_gpu": 99}}).encode()
        req = urllib.request.Request(OLLAMA, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as r:
            d = json.loads(r.read())
        ans = (d.get("response") or "").strip()
        if not ans or ans.startswith("不知道") or ans.startswith("无法"):
            return False, "", time.perf_counter() - t0
        if len(ans) > MAX_ANSWER_LEN:
            ans = ans[: MAX_ANSWER_LEN - 1] + "…"
        return True, ans, time.perf_counter() - t0
    except Exception:
        return False, "", time.perf_counter() - t0


def answer(text):
    """问句 -> (hit, answer, dt). hit=False 表示不是识别得到的问句, 走分拣路径."""
    t0 = time.perf_counter()
    t = (text or "").strip()
    if not t or _MATER_RE.search(t):
        return False, "", 0.0
    if not _QA_KW.search(t):
        return False, "", 0.0

    try:
        wd = re.search(r"(?:星期|周|礼拜)几", t)
        if wd:
            ans = _ans_weekday(t)
            if ans is None:
                return False, "", 0.0
        elif _TIME_KW.search(t):
            ans = _ans_time()
        elif _YARD_Q_RE.search(t) and re.search(r"多少|几件|还有|状态|空吗", t):
            ans = _ans_yard(t)
        elif any(pat.search(t) for pat, _, _ in _PORT_KW) or re.search(
                r"服务|进程|端口|还在跑|跑吗|在吗|正常吗", t):
            ans = _ans_service(t)
        elif _RES_KW.search(t):
            ans = _ans_resource(t)
        else:
            return False, "", 0.0
        if time.perf_counter() - t0 > QUERY_BUDGET_S:
            ans = "查询超时，请说分拣指令"
        if ans and len(ans) > MAX_ANSWER_LEN:
            ans = ans[: MAX_ANSWER_LEN - 1] + "…"
        return True, ans, time.perf_counter() - t0
    except Exception:
        return True, "查询失败，请说分拣指令", time.perf_counter() - t0
