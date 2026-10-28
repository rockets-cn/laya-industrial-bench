"""Industrial decision loop benchmark (no LLM in the real-time path):

    audio -> ASR -> rule normalization -> Laya (structured choice over the
    frozen 4-action whitelist) -> deterministic validator -> PLC dispatch

LLM (qwen3.5, LLM_MODEL env, default qwen3.5:4b) is BYPASS-ONLY:
called ONLY when the validator rejects; its output must pass the SAME
validator; on any failure the safe default is PAUSE + HUMAN CONFIRM.

Usage: .venv/bin/python bench_industrial.py [reps] [voice|text]
"""

import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_pipeline import LAYA, llm_json  # noqa: E402

REPS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
MODE = sys.argv[2] if len(sys.argv) > 2 else "voice"
AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_audio")
HERE = os.path.dirname(os.path.abspath(__file__))
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b")

# ---- frozen action set (the PLC's whitelist of commands) ----
ACTIONS = ["red_apple", "green_apple", "banana", "pear"]
NAME2ACTION = {"苹果": "red_apple", "红苹果": "red_apple", "绿苹果": "green_apple",
               "香蕉": "banana", "梨": "pear"}

# ---- rule normalization (deterministic, domain-closed) ----
T2S = str.maketrans("個紅給那谢", "个红给那谢")  # placeholder, fixed below
T2S = str.maketrans({"個": "个", "紅": "红", "給": "给", "那": "那", "谢": "谢",
                     "離": "离"})
HOMOPHONE = {"离": "梨", "禮": "梨", "李": "梨"}  # ASR 同音字 -> 领域词
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
NEG_PAT = re.compile(r"(?:不要|别要?|不需要|不用)")


def _qty_before(text, pos):
    pre = text[max(0, pos - 10):pos]
    m = re.search(r"([一两二三四五六七八九十\d]+)\s*(?:个|根|把|条|颗|只)?\s*$", pre)
    if not m:
        return 1
    tok = m.group(1)
    return CN_NUM.get(tok, int(tok) if tok.isdigit() else 1)


def normalize(text):
    """Deterministic: 简繁/同音字 -> 否定剥离 -> 提及+数量抽取."""
    text = text.translate(T2S)
    for a, b in HOMOPHONE.items():
        text = text.replace(a, b)
    # strip negated mentions: 不要梨 / 梨不要
    # 否定词与宾语之间限 0-3 字且不得跨另一个水果名 (无标点文本也安全)
    negated = set()
    for name in ("香蕉", "苹果", "梨"):
        pat = re.compile(rf"(?:不要|别要?|不需要|不用)(?:(?!苹果|香蕉|梨).){{0,3}}{name}|{name}(?:不要|不用)")
        if pat.search(text):
            negated.add(name)
            text = pat.sub("", text)
    mentions = []
    for m in re.finditer(r"(?:红|绿)?苹果|香蕉|梨", text):
        name = m.group(0)
        if "苹果" in name:
            base = "红苹果" if name.startswith("红") and "红" in name else ("绿苹果" if "绿" in name else "苹果")
        else:
            base = name
        mentions.append({"name": base, "action": NAME2ACTION[base],
                         "qty": _qty_before(text, m.start()), "negated": False})
    return text, mentions, sorted(negated)


FRUIT_CRIT = {"red_apple": "红苹果 (red apple): 红色, 圆形",
              "green_apple": "绿苹果 (green apple): 绿色, 圆形",
              "banana": "香蕉 (banana): 黄色, 长条, 弯曲",
              "pear": "梨 (pear): 上窄下宽, 多汁"}


def laya_decide(mentions):
    """One Laya request PER mention.666 per-mention state (measured: 1.0 conf
    for pear/banana, 0.96 for apple; multi-question single request is
    unstable). Returns (dt, answers) with answers keyed m0..mN-1."""
    answers, t0 = {}, time.perf_counter()
    for i, m in enumerate(mentions):
        qs = {"q": {"type": "choice",
                    "instructions": "用户要哪种水果?",
                    "criteria": FRUIT_CRIT}}
        body = json.dumps({"state": "用户要货: " + m["name"] + " x " + str(m["qty"]),
                           "model": "jev-latest", "questions": qs}).encode()
        req = urllib.request.Request(LAYA, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        answers["m" + str(i)] = d["answers"]["q"]
    return time.perf_counter() - t0, answers


def validate(mentions, answers):
    """Deterministic whitelist + cross-check + ranges. No models.
    answers=None (LLM bypass path): whitelist + range checks only."""
    actions, reasons = [], []
    for i, m in enumerate(mentions):
        choice = m["action"]
        if answers is not None:
            a = answers.get("m" + str(i))
            if a is None:
                reasons.append(m["name"] + ": 缺少决策输出")
                continue
            choice = a.get("choice")
            if choice not in ACTIONS:
                reasons.append(m["name"] + ": 动作不在白名单 " + repr(choice))
                continue
            if choice != m["action"]:
                reasons.append(m["name"] + ": 规则/决策冲突 " + m["action"] + " vs " + choice)
                continue
        if not (1 <= m["qty"] <= 99):
            reasons.append(m["name"] + ": 数量越界 " + str(m["qty"]))
            continue
        actions.append({"action": choice, "qty": m["qty"]})
    if not actions and not reasons:
        reasons.append("空订单")
    return (not reasons), actions, reasons


BYPASS_PROMPT = """你是要货解析器。把这句话解析成 JSON, 只输出 JSON:
{{"items":[{{"name":"苹果|香蕉|梨","qty":1}}]}}
"不要X" 表示 X 不下单, 不列入 items; 数量没说按 1。
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
        name = it.get("name", "")
        if name not in NAME2ACTION:
            reasons.append(f"LLM 项 {name!r} 不在白名单")
            continue
        qty = it.get("qty", 1)
        if not isinstance(qty, int) or not (1 <= qty <= 99):
            reasons.append(f"LLM 数量 {qty!r} 非法")
            continue
        mentions.append({"name": name, "action": NAME2ACTION[name], "qty": qty,
                         "negated": False})
    # hallucination guard: LLM may only order what the rule layer heard
    heard = {m["name"] for m in rule_mentions}
    dropped = [m["name"] for m in mentions if m["name"] not in heard]
    for d in dropped:
        reasons.append("LLM 幻觉项(规则层未听到): " + d)
    mentions = [m for m in mentions if m["name"] in heard]
    return dt, mentions, reasons


CASES = [
    ("我要那个苹果，我要那个梨，给我十个香蕉",
     {("red_apple", 1), ("pear", 1), ("banana", 10)}),
    ("来两个红苹果和一把香蕉", {("red_apple", 2), ("banana", 1)}),
    ("只给我三个梨就行", {("pear", 0)}),  # fixed below
    ("一个苹果，不要梨，五个香蕉谢谢", {("red_apple", 1), ("banana", 5)}),
]
CASES[2] = ("只给我三个梨就行", {("pear", 3)})

def _fix_negation_punct(text):
    """SenseVoice 常丢否定词前的逗号: "苹果不要梨" 实为 "苹果, 不要梨".
    仅在否定词后还有宾语时补逗号 (句尾 "梨不要" 仍是否定前词, 不动)."""
    return re.sub(r"(苹果|香蕉|梨)(?=(?:不要|别要?|不需要|不用)\s*[\u4e00-\u9fff0-9])", r"\1，", text)


def _voxtype_asr_choice():
    """ASR selection: SenseVoice small int8 (239MB, fast on CPU). Falls back to
    the fp32 build when the int8 weights are missing."""
    base = os.path.expanduser("~/.local/share/voxtype/models")
    if os.path.exists(os.path.join(base, "sensevoice-small", "model.int8.onnx")):
        return "sensevoice", "sensevoice-small"
    return "sensevoice", "sensevoice-small-fp32"


LAST_ASR_MODEL = "?"
LAST_ASR_ENGINE = "?"


def _ensure_voxtype_cfg():
    """Pinned voxtype config for this deployment's ASR model. Rebuilt from the
    user's global config when missing or drifted, so a TUI edit of the global
    config cannot break the bench/UI pipeline."""
    global LAST_ASR_MODEL, LAST_ASR_ENGINE
    engine, want = _voxtype_asr_choice()
    LAST_ASR_MODEL = want
    LAST_ASR_ENGINE = engine
    cfg = os.path.join(HERE, "voxtype_bench.toml")
    try:
        with open(cfg) as f:
            text = f.read()
    except FileNotFoundError:
        src = os.path.expanduser("~/.config/voxtype/config.toml")
        text = open(src).read() if os.path.exists(src) else '[sensevoice]\nlanguage = "auto"\n'
    # Pin the model inside the [engine] section only (other sections may carry
    # their own `model = ...` keys).
    out, in_sec, found = [], False, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_sec = s.lower().lstrip("[").rstrip("]").split(".")[0] == engine
        elif in_sec and s.startswith("model") and "=" in s:
            line, found = 'model = "%s"' % want, True
        out.append(line)
    text = "\n".join(out) + ("\n" if text.endswith("\n") or text == "" else "\n")
    if not found:
        text += '\n[%s]\nmodel = "%s"\n' % (engine, want)
    with open(cfg, "w") as f:
        f.write(text)
    return cfg


_REC = None


def _resident_recognizer():
    """In-process SenseVoice via sherpa-onnx: model loads ONCE (~0.7s) and stays
    resident, so each transcription is ~100-250 ms instead of paying process +
    model-load overhead (~750 ms) on every call, as the voxtype CLI does."""
    global _REC
    if _REC is None:
        import sherpa_onnx
        engine, model = _voxtype_asr_choice()
        assert engine == "sensevoice", f"resident engine only supports sensevoice, got {engine}"
        mdir = os.path.expanduser(f"~/.local/share/voxtype/models/{model}")
        mf = "model.int8.onnx" if model.endswith("-small") else "model.onnx"
        _REC = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            tokens=f"{mdir}/tokens.txt", model=f"{mdir}/{mf}",
            num_threads=4, language="zh", use_itn=False)
    global LAST_ASR_MODEL, LAST_ASR_ENGINE
    LAST_ASR_ENGINE, LAST_ASR_MODEL = _voxtype_asr_choice()
    return _REC


def _asr_cli(wav):
    """Fallback: voxtype CLI (cold process + model load per call)."""
    cfg = _ensure_voxtype_cfg()
    r = subprocess.run(["voxtype", "-q", "-c", cfg, "transcribe",
                        "--engine", LAST_ASR_ENGINE, wav],
                       capture_output=True, text=True, timeout=120)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    return lines[-1].strip() if lines else ""


def asr_warmup():
    """Load the resident ASR model now (once, ~0.7s) instead of on the first
    real utterance. Safe to call from server startup; no-op on failure."""
    try:
        _resident_recognizer()
    except Exception:
        pass


def asr(wav):
    """ASR: resident SenseVoice (sherpa-onnx, in-process), CLI fallback.
    Input must be 16 kHz mono WAV. Returns (seconds, text)."""
    t0 = time.perf_counter()
    try:
        import wave, array
        rec = _resident_recognizer()
        with wave.open(wav, "rb") as w:
            a = array.array("h")
            a.frombytes(w.readframes(w.getnframes()))
        st = rec.create_stream()
        st.accept_waveform(16000, [x / 32768.0 for x in a])
        rec.decode_stream(st)
        text = st.result.text
    except Exception:
        text = _asr_cli(wav)
    return time.perf_counter() - t0, _fix_negation_punct(text)


def main():
    print(f"mode={MODE} reps={REPS} llm_bypass={LLM_MODEL} (仅校验拒绝时调用)")
    wavs = ([os.path.join(AUDIO_DIR, f"tts_{i+1}.wav") for i in range(4)]
            if MODE == "voice" else None)
    if wavs:
        dt, t = asr(wavs[0])
        print(f"ASR warmup: {dt:.1f}s  '{t}'\n")

    t_asr, t_laya, t_bypass, t_e2e = [], [], [], []
    n_fast = n_bypass = n_pause = n_correct = 0
    for rep in range(REPS):
        for ci, (gt_text, expected) in enumerate(CASES):
            t0 = time.perf_counter()
            if MODE == "voice":
                dt, text = asr(wavs[ci])
                t_asr.append(dt)
            else:
                text, dt = gt_text, 0.0
            norm, mentions, neg = normalize(text)
            dt_l, answers = laya_decide(mentions)
            t_laya.append(dt_l)
            ok, actions, reasons = validate(mentions, answers)
            path, dt_b = "FAST", 0.0
            if not ok:
                dt_b, b_mentions, b_reasons = llm_bypass(norm, mentions)
                t_bypass.append(dt_b)
                if b_mentions:
                    ok_b, actions, reasons = validate(b_mentions, None)
                else:
                    ok_b, actions, reasons = False, [], b_reasons or ["LLM 无输出"]
                path = "BYPASS" if ok_b else "PAUSE+HUMAN"
            e2e = time.perf_counter() - t0
            t_e2e.append(e2e)
            got = frozenset((a["action"], a["qty"]) for a in actions)
            correct = got == frozenset(expected)
            n_correct += correct
            if path == "FAST":
                n_fast += 1
            elif path == "BYPASS":
                n_bypass += 1
            else:
                n_pause += 1
            print(f"rep{rep} c{ci} [{path:12s}] {'OK' if correct else 'WRONG'}"
                  f"{'  ' + text if MODE == 'voice' else ''}")
            if MODE == "voice":
                print(f"      asr='{text}'  neg={neg}")
            print(f"      actions={actions}  reasons={reasons[:3]}")

    n = REPS * len(CASES)
    print(f"\n===== 工业回路汇总 ({MODE}, {REPS} 轮 x {len(CASES)} 句) =====")
    print(f"路径: 快路径 {n_fast}/{n}   LLM旁路 {n_bypass}/{n}   暂停+人工 {n_pause}/{n}")
    print(f"最终正确: {n_correct}/{n} = {100*n_correct/n:.0f}%")
    if t_asr:
        print(f"ASR:      mean {statistics.mean(t_asr)*1000:.0f} ms")
    print(f"Laya:    mean {statistics.mean(t_laya)*1000:.0f} ms  max {max(t_laya)*1000:.0f} ms")
    if t_bypass:
        print(f"LLM旁路:  mean {statistics.mean(t_bypass)*1000:.0f} ms (仅触发时)")
    print(f"E2E:      mean {len(t_e2e)} 次: mean {statistics.mean(t_e2e)*1000:.0f} ms  "
          f"p95 {sorted(t_e2e)[int(.95*len(t_e2e))]*1000:.0f} ms  max {max(t_e2e)*1000:.0f} ms")


if __name__ == "__main__":
    main()
