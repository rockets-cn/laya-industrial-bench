"""本机运行状态简单问答 (纯规则层, 零 LLM, 毫秒级).

定位: 料场分拣回路收到"问句"而非"分拣指令"时的兜底回答能力.
硬约束:
- 答案 <= 30 字 (超出截断);
- 查询全程有超时预算 (端口探测 0.4s/个, 总预算 3s), 超时答"查询超时";
- 只回答能确定性取得的事实; 识别不出类别 -> 不答 (return hit=False),
  原句继续走分拣路径 (空订单 -> PAUSE+HUMAN), 绝不编造.

类别:
1. 时间/日期   "现在几点了" / "今天星期几" / "明天是星期几" (相对日期的星期)
2. 料场状态   a) 「1号料场有多少件」= 该料场 累计库存 (SQLite 台账, 默认答案;
                空仓答「暂无库存」, 含 物料明细 top2)
             b) 「最近一次 1号料场的指令有多少」= 最近一次分拣指令的库存
                (显式 最近/上次/刚才 才进; 由 loop_ui 每次 /run 后注入)
             c) 「现在各个仓库分别有多少内容」/「仓库库存」= 全部料场 累计库存
                (门 _STOCK_RE: 各/分别/库存/仓库里有 等; 与 a 的区别: 不分料场)
             d) 「哪个仓库有人体红外传感器」/「角度传感器在哪个仓库里」= 物料→仓库 反查
                (台账, 哪些料场有该物料库存; 门: 物料词 + 哪 + 有/在/库存;
                每类传感器独立, 无颜色家族)
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
import threading
import time
import urllib.request

# 出入库台账 (yard_ledger.py, SQLite): 累计库存问答的数据源.
# 直接 import; 若 sys.path 不含本目录 (被按文件路径单独加载时), 退回按路径加载.
try:
    import yard_ledger
except Exception:
    import importlib.util as _ilu
    _sp = _ilu.spec_from_file_location(
        "yard_ledger",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "yard_ledger.py"))
    yard_ledger = _ilu.module_from_spec(_sp)
    _sp.loader.exec_module(yard_ledger)

MAX_ANSWER_LEN = 30      # 答案硬上限 (字)
QUERY_BUDGET_S = 3.0    # 整个问答的耗时预算
PORT_PROBE_S = 0.4      # 单端口 TCP 探测超时

# 分拣物料词: 句子里出现物料名 -> 视为分拣指令, 不进问答 (防误抢)
_MATER_RE = re.compile(r"角度传感器|超声波传感器|人体红外传感器|人体热释电传感器|热释电传感器"
                       r"|温湿度传感器|温度湿度传感器"
                       r"|角度|超声波|人体红外|人体热释电|热释电|红外|温湿度|温度湿度|旋钮")
# 料场号
_YARD_Q_RE = re.compile(r"([123一二两])\s*号\s*(?:料场|仓库|场地|库|位置|区)?")
_YARD_NUM = {"1": 1, "2": 2, "3": 3, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3}
# 累计库存问句门 (各/分别/库存/仓库里有...): 答 台账 累计库存, 区别于 _YARD_Q_RE 的
# 「N号料场有多少」= 最近一次分拣指令 (见 _ans_yard). 要求 仓库/料场 语境, 防误抢;
# 且 带 显式料场号 (N号) 的问句 不进门 (走 最近指令 分支, 保持 c25 语义).
# 2026-11-20: 补「库」单字语境 (「现在库里有多少东西」实测落到了 LLM 兜底, 慢且多余)
_STOCK_RE = re.compile(
    r"各(个)?(?:仓库|料场|库)"
    r"|(?:仓库|料场|库)(?:里|内|中)?(?:都有|总有|都|有)?(?:多少|什么|几)"
    r"|库存"
    r"|(?:总|一)共有多少|总共多少|一共多少")
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
    r"内存|硬盘|磁盘|网络|CPU|cpu|服务|进程|端口|回答|是什么|什么|状态|还有|跑吗|在吗|哪")

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


def _ans_stock_ledger():
    """累计库存 (出入库台账): 现在各仓库分别有多少内容 — 数据源 yard_ledger SQLite
    (分拣下发自动记入, 页面手动记出; 库存 = 入 - 出). 台账读不到时返回 None -> 不答.
    与 _ans_yard (最近一次分拣指令) 的区别: 这里答的是 累计 存量."""
    try:
        s = yard_ledger.stock()
    except Exception:
        return None
    if s["moves"] == 0:
        return "暂无库存，共0件"
    parts = [f"{y}号{s['yards'][y]['total']}件" for y in (1, 2, 3)]
    return " ".join(parts) + f"，共{s['grand_total']}件"


def _ans_yard_stock(text):
    """指定料场的 累计库存 (台账): 「一号仓库里有多少货」/「3号仓库现在有多少货」—
    数据源 yard_ledger SQLite (入-出, 含物料明细 top2). 这是「N号有多少」的 默认 答案;
    仅当显式问 最近/上次/刚才 那单时, 才走 _ans_yard (最近一次分拣指令)."""
    m = _YARD_Q_RE.search(text)
    if not m:
        return None
    y = _YARD_NUM.get(m.group(1))
    if y not in (1, 2, 3):
        return None
    try:
        s = yard_ledger.stock()
    except Exception:
        return None
    d = s["yards"][y]
    tot = d["total"]
    if tot <= 0 and not d["items"]:
        return f"{y}号仓库: 暂无库存, 0件"
    top = sorted(d["items"].items(), key=lambda kv: -kv[1])[:2]
    detail = " (" + " ".join(f"{_MATCN.get(k, k)} x{v}" for k, v in top) + ")" if top else ""
    return f"{y}号仓库: {tot}件{detail}"


def _ans_mat_where(text):
    """物料→仓库 反查: 「哪个仓库有人体红外传感器？」/「角度传感器在哪个仓库里？」— 查台账
    (yard_ledger) 哪些料场 有该物料 净库存 (>0). 门: 物料词 + 哪 + (有|在|库存|存),
    避免误抢 分配指令 (「放到哪个仓库」类无 有/在 库存语义); 全仓 0 答「没有」.
    简称与全名同 key (「红外」= 人体红外传感器)."""
    m = _MAT_WHERE_PAT.search(text)
    if not m:
        return None
    mats, cn = _mat_where_family(m.group(0))
    try:
        s = yard_ledger.stock()
    except Exception:
        return None
    hits = []
    for y in (1, 2, 3):
        items = s["yards"].get(y, {}).get("items", {})
        n = sum(items.get(k, 0) for k in mats)
        if n > 0:
            hits.append((y, n))
    if not hits:
        return f"仓库里目前没有{cn}"
    if len(hits) == 1:
        return f"{cn}: 在{hits[0][0]}号仓库, {hits[0][1]}件"
    parts = " ".join(f"{y}号({n}件)" for y, n in hits)
    return f"{cn}在: {parts}"


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
    det = {}
    for a in items:
        if a.get("material") in _MATCN:
            det[a["material"]] = det.get(a["material"], 0) + a.get("qty", 0)
    detail = " ".join(f"{_MATCN[m]}x{n}" for m, n in det.items())
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


_MATCN = {"angle_sensor": "角度传感器", "ultrasonic_sensor": "超声波传感器",
          "pir_sensor": "人体红外传感器", "temp_humidity_sensor": "温湿度传感器"}

# 物料反查用: 全名/口语简称都认 (「人体红外」=「人体红外传感器」); 每类独立, 无颜色家族
_MAT_WHERE_PAT = re.compile(
    r"角度传感器|超声波传感器|人体红外传感器|人体热释电传感器|热释电传感器"
    r"|温湿度传感器|温度湿度传感器"
    r"|角度|超声波|人体红外|人体热释电|热释电|红外|温湿度|温度湿度|旋钮")

# 口语形态 -> 物料 key (简称与全名同 key)
_MAT_WHERE_KEY = {
    "角度传感器": "angle_sensor", "角度": "angle_sensor", "旋钮": "angle_sensor",
    "超声波传感器": "ultrasonic_sensor", "超声波": "ultrasonic_sensor",
    "人体红外传感器": "pir_sensor", "人体红外": "pir_sensor",
    "人体热释电传感器": "pir_sensor", "人体热释电": "pir_sensor",
    "热释电传感器": "pir_sensor", "热释电": "pir_sensor",
    "红外传感器": "pir_sensor", "红外": "pir_sensor",
    "温湿度传感器": "temp_humidity_sensor", "温湿度": "temp_humidity_sensor",
    "温度湿度传感器": "temp_humidity_sensor", "温度湿度": "temp_humidity_sensor"}


def _mat_where_family(word):
    """口语形态 -> (物料 key 列表, 显示名). 传感器每类独立: 简称/全名 -> 同一 key."""
    return [_MAT_WHERE_KEY[word]], _MATCN[_MAT_WHERE_KEY[word]]


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
#   - 物料词 (角度/超声波/红外/温湿度 等传感器) 一律视为分拣指令, 不进问答 (含 LLM 兜底)。
# 开关: 环境变量 QA_LLM=1 (服务启动时生效, 如 QA_LLM=1 python3 loop_ui.py);
#       模型可用 QA_LLM_MODEL 覆盖 (默认 qwen3.5:4b, 与旁路同款)。
# ---------------------------------------------------------------------------
OLLAMA = "http://127.0.0.1:11434/api/generate"
# 模型: QA_LLM_MODEL 专用覆盖; 否则跟随 LLM_MODEL (systemd drop-in llm-model.conf
# 统一切换, 2026-11-20 发现 QA 兜底没跟上旁路的 dsv4-qwen35:4b — 变量名不一致)
LLM_MODEL = (os.environ.get("QA_LLM_MODEL")
             or os.environ.get("LLM_MODEL", "qwen3.5:4b"))
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
    # 累计库存 (出入库台账) — LLM 兜底问答的注入事实
    try:
        _s = yard_ledger.stock()
        _parts = " ".join(f"{y}号{_s['yards'][y]['total']}件" for y in (1, 2, 3))
        lines.append(f"当前仓库库存(累计入-出): {_parts}，共{_s['grand_total']}件")
    except Exception:
        pass
    # 分拣线运行态 (网关 :8123 /health, 本机 loopback): 带态/故障/队列/逐件核对进度,
    # 供 LLM 兜底答「带子停了吗/有故障吗/任务还剩几件」. 取不到/超时 -> 整行静默跳过,
    # 不影响其余事实 (端口未监听时 loopback 拒连是毫秒级; 网关忙时 0.5s 超时兜底).
    try:
        with urllib.request.urlopen("http://127.0.0.1:8123/health",
                                    timeout=0.5) as r:
            _h = json.loads(r.read())
        _conv = {"running": "运行", "stopped": "停止", "paused": "暂停",
                 "fault": "故障"}.get(_h.get("conveyor"), "状态未知")
        _line = f"分拣线: 带子{_conv}"
        _fault = _h.get("fault")
        if isinstance(_fault, dict):
            _line += (f", 故障锁存 ({_fault.get('device', '?')} "
                      f"{_fault.get('error', '?')})")
        elif _fault:
            _line += ", 故障锁存"
        else:
            _line += ", 无故障"
        _line += f", 队列等待 {_h.get('queue', 0)} 批"
        _vs = _h.get("verified") or []
        if _vs:
            _p = "; ".join(
                f"{_MATCN.get(v.get('material'), v.get('material') or '?')}"
                f" 已推 {v.get('pushed', 0)}/{v.get('pushed', 0) + v.get('remaining', 0)}"
                for v in _vs)
            _line += f"; 核对中: {_p}"
        lines.append(_line)
    except Exception:
        pass
    return "\n".join(lines)


# 兜底层的问句判定: 规则层关键词表 (_QA_KW) 或 中文疑问词 — 比规则层宽,
# 因为本层就是为 规则层认不出的 问句 兜底; 分拣指令仍被物料词门拦在外面。
_INTERROG_RE = re.compile(
    r"吗|呢|什么|啥|干吗|干什么|干啥|怎么|为什么|哪个|哪一|是谁|几[个种次号点日]|多少|有无|能否|可否")


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
    # 物料词 = 分拣指令, 绝不进问答 (与规则层同门); 非问句不答。
    # 例外: 带「哪」的物料问句 = 反查查询 (「哪个仓库有人体红外传感器」), 放行给 _ans_mat_where
    if not t or (_MATER_RE.search(t) and not re.search(r"哪", t)):
        return False, "", 0.0
    # 身份/名字 (2026-11-20 用户定案: 固定答「工业机本地问答助手优优」= 唤醒词同名;
    # 在 _QA_KW 门之前 — 「你是谁」无任何问答关键词)
    if re.search(r"名字|你是谁|叫什么|你是哪位", t):
        return True, "工业机本地问答助手优优", time.perf_counter() - t0
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
        elif _STOCK_RE.search(t) and not _YARD_Q_RE.search(t):
            # 累计库存 (台账): 「现在仓库里有多少东西」等 — 先于 单料场最近指令;
            # 带 显式料场号 (N号...) 的问句 留给 最近指令 分支
            ans = _ans_stock_ledger()
            if ans is None:
                return False, "", 0.0
        elif _MAT_WHERE_PAT.search(t) and re.search(r"哪", t) and re.search(
                r"有|在|库存|存", t):
            # 物料→仓库 反查: 「哪个仓库有人体红外传感器」— 查台账哪些料场有该物料库存
            ans = _ans_mat_where(t)
            if ans is None:
                return False, "", 0.0
        elif _YARD_Q_RE.search(t) and re.search(r"最近|上次|刚才", t) and re.search(
                r"多少|几件|什么|内容|指令|货", t):
            # 显式问「最近/上次那单」→ 最近一次分拣指令的库存 (loop_ui 注入)
            ans = _ans_yard(t)
        elif _YARD_Q_RE.search(t) and re.search(r"多少|几件|还有|状态|空吗|有.*货|什么", t):
            # 「N号仓库有多少货」默认答 该料场 累计库存 (台账) — 2026-10-29 用户语音实测:
            # 「一号仓库里有多少货？」期望 库存 而非「最近一次指令」
            ans = _ans_yard_stock(t)
            if ans is None:
                return False, "", 0.0
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


# ---------------------------------------------------------------------------
# 已学问答库 (2026-11-20, 用户定案): LLM 兜底成功回答的静态问答自动入库
# (data/qa_learned.jsonl); 下次同义问题绕开 4b (10~16s) 秒回:
#   1) 归一化精确匹配 (去标点/语气词; 命中 原问 或 教师变体);
#   2) Laya 选择题同义路由 (criteria=已学问题+变体, 选中即答, none=都不像).
# 教师增强: 入库后后台守护线程让 4b 造 4 个同义问法补进条目 (变体只影响
#   匹配命中率, 不改答案); 教师失败静默 — 条目本体入库即已可用。
# 纪律: ① 动态事实问答 (库存/故障/多少/几点 等) 不学 — 缓存会说过期话,
#        那些必须走实时事实; ② 物料词 = 分拣指令, 不抢; ③ 命中仅展示,
#        与 QA 同纪律 — 无 PLC 输出。
# ---------------------------------------------------------------------------
LEARN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "qa_learned.jsonl")
LEARN_MAX = 200          # 库上限: 超出丢最旧
LEARN_ROUTE_TOP = 30     # Laya 同义路由最多带最近 N 条 (选择题选项不宜过长)
LAYA_URL = os.environ.get(
    "LAYA_URL", "http://127.0.0.1:8000/v1/systemone")  # 与 bench_pipeline.LAYA 同端点
LEARN_TIMEOUT_S = 15.0   # Laya 同义路由超时 (实测 ~0.2s; 超时回落 LLM 兜底)
TEACHER_TIMEOUT_S = 30.0  # 教师造变体超时 (后台线程, 不阻塞主路径)

_DYNAMIC_Q_RE = re.compile(
    r"多少|几件|库存|故障|几点|星期|日期|时间|负载|内存|占用|磁盘|还剩|"
    r"停了吗|在跑|还在|运行|温度|湿度|正常吗")
_NORM_STRIP_RE = re.compile(r"[\s，,。．.？?！!；;：:、~～呢啊呀吧嘛哦哈的]+")
_learn_lock = threading.Lock()
_learn_cache = {"mtime": None, "entries": []}


def _norm_q(t):
    return _NORM_STRIP_RE.sub("", (t or "").strip())


def _load_learned():
    """读学习库 (jsonl), mtime 缓存. 返回 [{q, ans, ts, variants?}], 旧→新."""
    try:
        mt = os.path.getmtime(LEARN_PATH)
    except OSError:
        _learn_cache.update(mtime=None, entries=[])
        return []
    if _learn_cache["mtime"] != mt:
        entries = []
        try:
            with open(LEARN_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("q") and e.get("ans"):
                        entries.append(e)
        except OSError:
            entries = []
        _learn_cache.update(mtime=mt, entries=entries)
    return _learn_cache["entries"]


def _save_learned(entries):
    """整库重写 (jsonl 行序=入库序). 调用方持锁."""
    os.makedirs(os.path.dirname(LEARN_PATH), exist_ok=True)
    with open(LEARN_PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    _learn_cache["mtime"] = None  # 强制下次重读


_TEACHER_PROMPT = (
    "把下面这个问题改写成 4 个意思完全相同的日常问法, 每行一个, "
    "只输出问句本身, 不要编号不要解释:\n问题: {q}\n问法:")


def _teacher_variants(q):
    """教师增强: 4b 造同义问法. 返回 [str] (<=4, 已清洗去重); 失败返回 []."""
    try:
        body = json.dumps({"model": LLM_MODEL,
                           "prompt": _TEACHER_PROMPT.format(q=q),
                           "stream": False, "think": False,
                           "options": {"temperature": 0.3, "num_predict": 128,
                                       "num_gpu": 99}}).encode()
        req = urllib.request.Request(OLLAMA, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TEACHER_TIMEOUT_S) as r:
            d = json.loads(r.read())
        out = []
        for ln in (d.get("response") or "").splitlines():
            ln = re.sub(r"^\s*(?:\d+[\.\、\)]|[-*•]|问法\d*[:：])\s*", "", ln).strip()
            if not ln or len(ln) > 30:
                continue
            if _DYNAMIC_Q_RE.search(ln):
                continue  # 教师造出动态问法 = 跑偏, 不收
            if _norm_q(ln) == _norm_q(q):
                continue  # 与原问同形, 不算变体 (2026-11-20 实测教师复述原句)
            if _norm_q(ln) not in [_norm_q(v) for v in out]:
                out.append(ln)
            if len(out) >= 4:
                break
        return out
    except Exception:
        return []


def _teacher_thread(nq):
    """后台: 造变体 -> 补写进目标条目 (按归一化问句定位; 条目被清则放弃)."""
    entries = _load_learned()
    target = next((e for e in entries if _norm_q(e["q"]) == nq), None)
    if target is None:
        return
    variants = _teacher_variants(target["q"])
    if not variants:
        return
    with _learn_lock:
        entries = _load_learned()
        target = next((e for e in entries if _norm_q(e["q"]) == nq), None)
        if target is None:
            return
        target["variants"] = variants
        try:
            _save_learned(entries)
        except OSError:
            pass


def learn_from_llm(q, ans):
    """LLM 兜底命中后自动入库 (幂等: 归一化同问覆盖旧条), 并起后台教师线程
    造同义变体. 动态事实问答不学 (库存/故障等必须实时查)."""
    if not q or not ans:
        return
    if _DYNAMIC_Q_RE.search(q):
        return
    nq = _norm_q(q)
    with _learn_lock:
        entries = [e for e in _load_learned() if _norm_q(e["q"]) != nq]
        entries.append({"q": q.strip(), "ans": ans.strip(),
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        try:
            _save_learned(entries[-LEARN_MAX:])
        except OSError:
            return
    threading.Thread(target=_teacher_thread, args=(nq,), daemon=True).start()


def answer_learned(text):
    """已学问答库: (hit, answer, dt). 两级: 归一化精确 (含教师变体) → Laya
    同义路由. 任何一步异常都回落 (hit=False) 交给 LLM 兜底, 绝不阻断主路径."""
    t0 = time.perf_counter()
    t = (text or "").strip()
    if not t:
        return False, "", 0.0
    if _MATER_RE.search(t) and not re.search(r"哪", t):
        return False, "", 0.0   # 物料词 = 分拣指令, 不抢 (与 answer() 同门)
    if not (_QA_KW.search(t) or _INTERROG_RE.search(t)):
        return False, "", 0.0
    entries = _load_learned()
    if not entries:
        return False, "", 0.0
    nq = _norm_q(t)
    for e in reversed(entries):  # 新的优先; 原问与变体都参与精确匹配
        cand = [e["q"]] + list(e.get("variants") or [])
        if any(_norm_q(c) == nq for c in cand):
            return True, e["ans"], time.perf_counter() - t0
    # Laya 选择题同义路由: 「用户问题与哪条已学问题同义?」
    pool = entries[-LEARN_ROUTE_TOP:]
    criteria, key2entry = {"none": "都不像, 不是同义问题"}, {}
    for i, e in enumerate(pool):
        key = f"q{i}"
        criteria[key] = " / ".join([e["q"]] + list(e.get("variants") or [])[:2])
        key2entry[key] = e
    try:
        body = json.dumps({
            "state": f"用户问题: {t}", "model": "jev-latest",
            "questions": {"q": {"type": "choice",
                                "instructions": "用户问题与下面哪条已学问题是"
                                                "同一个意思? 都不像就选 none",
                                "criteria": criteria}}}).encode()
        req = urllib.request.Request(LAYA_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LEARN_TIMEOUT_S) as r:
            d = json.loads(r.read())
        ans_q = d.get("answers", {}).get("q") or {}
        choice = ans_q.get("choice")
        # 置信门槛 (2026-11-20 实测): Laya 对首候选有位置偏置 — 弱匹配
        # (conf<0.5) 会错路由 (「支持什么物料」→「你会什么」conf 0.24);
        # 真同义 conf 0.6~0.8. 弱匹配宁可 miss 落 LLM 兜底, 也不错答.
        conf = float(ans_q.get("confidence") or 0.0)
        if (choice and choice != "none" and choice in key2entry
                and conf >= 0.5):
            return True, key2entry[choice]["ans"], time.perf_counter() - t0
    except Exception:
        pass
    return False, "", time.perf_counter() - t0
