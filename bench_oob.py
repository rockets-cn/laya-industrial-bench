"""Out-of-range acceptance cases (DEPLOY_SENSEVOICE.md §9 ③④⑤).

③ "把三号口的窗户关上" -> PAUSE+HUMAN (超纲, 无领域提及)
④ "不要香蕉"         -> PAUSE+HUMAN (幻觉项被拦截)
⑤ "给我来十个苹果"   -> FAST, qty=10
"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("bi", "bench_industrial.py")
bi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bi)

CASES = [
    ("把三号口的窗户关上", "PAUSE+HUMAN"),
    ("不要香蕉", "PAUSE+HUMAN"),
    ("给我来十个苹果", "FAST"),
]

ok_all = True
for text, expect in CASES:
    try:
        norm, mentions, neg = bi.normalize(text)
        dt_l, answers = bi.laya_decide(mentions)
        ok, actions, reasons = bi.validate(mentions, answers)
        path, dt_b = "FAST", 0.0
        if not ok:
            try:
                dt_b, b_mentions, b_reasons = bi.llm_bypass(norm, mentions)
                if b_mentions:
                    ok_b, actions, reasons = bi.validate(b_mentions, None)
                else:
                    ok_b, actions, reasons = False, [], b_reasons or ["LLM 无输出"]
                path = "BYPASS" if ok_b else "PAUSE+HUMAN"
            except Exception as e:
                path, actions, reasons = "PAUSE+HUMAN (LLM不可用)", [], [f"LLM error: {e}"]
        qty = actions[0]["qty"] if path == "FAST" and actions else None
        got_ok = (path == expect) and (expect != "FAST" or qty == 10)
        ok_all &= got_ok
        print(f"[{'PASS' if got_ok else 'FAIL'}] {text!r} -> {path} (expect {expect})"
              f"  actions={actions} reasons={reasons[:2]}")
    except Exception as e:
        ok_all = False
        print(f"[FAIL] {text!r} -> exception: {e}")

sys.exit(0 if ok_all else 1)
