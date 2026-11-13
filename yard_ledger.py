"""料场出入库台账 (SQLite, 简单持久化统计).

定位: 统计 放入/取出 各料场的物料 — 回答「执行了多少内容」.
- 入库 (in):  分拣回路 每次产生可执行下发 (FAST/EXPAND/PARTIAL) 时, 由 loop_ui
              按 每条 action 记一行 (自动, 语音/文本/预设 全来源);
- 出库 (out): 页面上的手动出库表单 (POST /ledger/out), path='MANUAL';
- 库存 = 累计入 - 累计出 (按 料场×物料 维度), 不为负时正常, 为负说明出大于入
              (仍如实显示, 由人工核对).

技术: SQLite (标准库 sqlite3), 文件 data/yard_ledger.db.
并发: 低并发场景 (控制台页面), 每次调用独立连接 + 5s 锁超时, 足够;
      写入是单条 INSERT, 不做长事务.

Schema:
    moves(id, ts, direction[in|out], yard[1-3], material, qty, path, note)
"""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "yard_ledger.db")

YARDS = (1, 2, 3)
# 物料白名单 (与 bench_yard.MATERIALS 一致: 4 种传感器; 此处独立维护, 台账不依赖分拣域)
MATERIALS = ("angle_sensor", "ultrasonic_sensor", "pir_sensor",
             "temp_humidity_sensor")

SCHEMA = """
CREATE TABLE IF NOT EXISTS moves(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                 -- 本地时间 'YYYY-MM-DD HH:MM:SS'
  direction TEXT NOT NULL CHECK(direction IN ('in','out')),
  yard INTEGER NOT NULL CHECK(yard BETWEEN 1 AND 3),
  material TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK(qty BETWEEN 1 AND 999),
  path TEXT,                        -- 入库路径 FAST/EXPAND/PARTIAL; 出库 'MANUAL'
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_moves_yard_ts ON moves(yard, ts);
"""


def _conn():
    c = sqlite3.connect(DB, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def init():
    """建目录 + 建表 (幂等). 模块导入即调用, 失败抛异常由调用方处理."""
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with _conn() as c:
        c.executescript(SCHEMA)


def record_in(actions, path, source="ui"):
    """入库记账: actions = [{yard, material, qty}, ...] (可执行下发的动作列表).
    返回记入的行数; 跳过非法行 (料场/物料/数量越界), 绝不中断主流程."""
    n = 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for a in actions or []:
        yard, mat, qty = a.get("yard"), a.get("material"), a.get("qty")
        if yard in YARDS and mat in MATERIALS and isinstance(qty, int) and 1 <= qty <= 999:
            rows.append((ts, "in", yard, mat, qty, path, source))
    if not rows:
        return 0
    with _conn() as c:
        c.executemany(
            "INSERT INTO moves(ts, direction, yard, material, qty, path, note)"
            " VALUES(?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows])
    return len(rows)


def record_out(yard, material, qty, note="手动出库"):
    """手动出库记账 (页面表单). 校验失败抛 ValueError, 由 API 层转 422."""
    if yard not in YARDS:
        raise ValueError("料场只接受 1/2/3")
    if material not in MATERIALS:
        raise ValueError(f"物料不在白名单: {material!r}")
    if not isinstance(qty, int) or not (1 <= qty <= 999):
        raise ValueError("数量需为 1~999 的整数")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute("INSERT INTO moves(ts, direction, yard, material, qty, path, note)"
                  " VALUES(?,?,?,?,?,?,?)", (ts, "out", yard, material, qty, "MANUAL", note))
    return True


def stock():
    """当前库存: 累计入 - 累计出 (按 料场×物料). 返回:
    {"yards": {1: {"total": n, "items": {"angle_sensor": n, ...}}, 2: ..., 3: ...},
     "grand_total": 各料场净库存合计 (可为负, 如实展示),
     "moves": 总流水行数}"""
    with _conn() as c:
        rows = c.execute(
            "SELECT yard, material,"
            " SUM(CASE WHEN direction='in'  THEN qty ELSE 0 END) AS i,"
            " SUM(CASE WHEN direction='out' THEN qty ELSE 0 END) AS o"
            " FROM moves GROUP BY yard, material").fetchall()
        total_moves = c.execute("SELECT COUNT(*) AS n FROM moves").fetchone()["n"]
    yards = {y: {"total": 0, "items": {}} for y in YARDS}
    for r in rows:
        net = r["i"] - r["o"]
        if r["yard"] in yards:
            yards[r["yard"]]["items"][r["material"]] = net
            yards[r["yard"]]["total"] += net
    return {"yards": yards,
            "grand_total": sum(v["total"] for v in yards.values()),
            "moves": total_moves}


def recent(n=15):
    """最近 n 条流水 (倒序, 最新在前)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, direction, yard, material, qty, path, note"
            " FROM moves ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]


def reset():
    """清空整个台账 (DELETE + VACUUM 缩回小文件). 服务运行中也可安全调用
    (SQLite 自动 5s 锁等待). 等价于删除 data/yard_ledger.db 再重启.
    注意: VACUUM 不能在事务内执行, 故 DELETE 先单独 commit."""
    c = _conn()
    try:
        c.execute("DELETE FROM moves")
        c.commit()
        c.execute("VACUUM")
    finally:
        c.close()
    return True


init()
