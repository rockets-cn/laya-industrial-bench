"""Full voice pipeline benchmark: WAV -> ASR -> LLM intent -> Laya finalize.

Stage 1 ASR:  faster-whisper (small, CPU int8)
Stage 2 LLM:  qwen3.5:4b via ollama (intent parse -> JSON)
Stage 3 Laya: each parsed item classified into the 4 fruit categories

Also reports ASR text accuracy (char match vs ground-truth text) so we can
attribute end-to-end misses to a specific stage.

Usage: .venv/bin/python bench_voice.py [reps]   (default 3)
"""

import difflib
import glob
import json
import os
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from faster_whisper import WhisperModel

from bench_pipeline import (FRUIT_CRIT, INTENT_CASES, INTENT_PROMPT, LAYA, MODEL,
                             OLLAMA, http_post, laya_choice, llm_json)

REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_audio")
WHISPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "whisper-small")

ITEM2CLS = {"苹果": "red_apple", "梨": "pear", "香蕉": "banana"}
CLS2ITEM = {v: k for k, v in ITEM2CLS.items()}

whisper = None


def asr(wav):
    global whisper
    if whisper is None:
        whisper = WhisperModel(WHISPER_DIR, device="cpu", compute_type="int8")
    t0 = time.perf_counter()
    segments, info = whisper.transcribe(wav, language="zh", beam_size=1,
                                        vad_filter=True)
    text = "".join(s.text for s in segments).strip()
    return time.perf_counter() - t0, text


def char_match(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def main():
    wavs = sorted(glob.glob(os.path.join(AUDIO_DIR, "tts_*.wav"))) or sorted(glob.glob(os.path.join(AUDIO_DIR, "intent_*.wav")))
    print(f"audio files: {len(wavs)}  reps={REPS}  asr=whisper-small(cpu)  llm={MODEL}")

    # warmup ASR (downloads model on first use)
    dt, text = asr(wavs[0])
    print(f"ASR warmup (model load+1st transcribe): {dt:.1f}s  text: {text}\n")

    asr_lat, pipe_lat = [], []
    for rep in range(REPS):
        for wi, wav in enumerate(wavs):
            ci = wi
            state_gt, exp = INTENT_CASES[ci]
            t0 = time.perf_counter()
            dt_asr, text = asr(wav)
            asr_lat.append(dt_asr)
            match = char_match(text, state_gt)
            dt_llm, parsed = None, None
            try:
                dt_llm, parsed = llm_json(INTENT_PROMPT.format(state=text))
            except Exception as e:
                print(f"  !! LLM failed: {e}")
            dt_laya = 0.0
            n_ok = n_tot = 0
            for fruit, (want, qty) in exp.items():
                name = CLS2ITEM[fruit]
                it = (parsed or {}).get("intents", [{}])
                it = next((x for x in it if x.get("item") == name), None)
                n_tot += 1
                if it is not None and bool(it.get("want")) == want:
                    n_ok += 1
            wall = time.perf_counter() - t0
            pipe_lat.append(wall)
            print(f"rep{rep} case{ci}: ASR {dt_asr*1000:6.0f}ms text[{match:.0%}] '{text}'"
                  + (f"  LLM {dt_llm*1000:.0f}ms" if dt_llm else "  LLM n/a")
                  + f"  want {n_ok}/{n_tot}  E2E {wall:.1f}s")

    asr_ms = sorted(x * 1000 for x in asr_lat)
    pl = sorted(pipe_lat)
    print("\n=== 语音流水线汇总 ===")
    print(f"ASR:      mean {statistics.mean(asr_ms):.0f} ms  p95 {asr_ms[int(.95*len(asr_ms))]:.0f} ms  "
          f"max {asr_ms[-1]:.0f} ms")
    print(f"E2E 语音->决策: mean {statistics.mean(pl):.1f} s  p95 {pl[int(.95*len(pl))]:.1f} s  max {pl[-1]:.1f} s")


if __name__ == "__main__":
    main()
