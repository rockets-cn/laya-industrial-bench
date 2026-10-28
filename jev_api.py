"""Jev-compatible HTTP API for a locally deployed Laya.

Speaks the TypeSafe "System One" wire format (https://docs.typesafe.ai/api):

    POST /v1/systemone
    Authorization: Bearer <LAYA_JEV_API_KEY>   (only when the env var is set)
    {
      "state":   str | object | array,
      "model":   "jev-latest" | "jev-english",
      "questions": { "<id>": {"type": "noul|choice|score", "instructions": ..., "criteria": ...} }
    }

    -> 200
    {
      "model": "jev-latest",
      "answers": {
        "<id>": {
          "type": "noul",   "noul": 0.95
        } | {
          "type": "choice", "choice": "billing", "probabilities": {...}, "confidence": 0.81
        } | {
          "type": "score",  "score": 1.05, "legend": {...}, "probabilities": {...}, "confidence": 0.92
        }
      },
      "usage": {"input_tokens": N, "output_tokens": N}
    }

Laya's native answers already carry choice/probabilities/confidence, noul, and
score/legend/probabilities/confidence, so this is a thin shim: it validates the
request, maps `model` onto a local checkpoint, trims laya-only fields, and reports
Jev-style errors (401 / 422).

Run:
    .venv/bin/python jev_api.py            # 0.0.0.0:8000
    HOST=127.0.0.1 PORT=9000 .venv/bin/python jev_api.py
"""

import json
import os
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import torch
from laya import Router

ROOT = Path(__file__).resolve().parent
MODELS = {
    "multilingual": str(ROOT / "models/laya/multilingual"),
    "english": str(ROOT / "models/laya"),
    # 离线教师微调产物 (teacher_offline.py train): 4 水果动作域, 留出集 100%.
    "fruits-teacher": str(ROOT / "models/laya-fruits-teacher"),
}
# Jev `model` name -> (checkpoint to force, name to echo back in the response).
MODEL_ALIASES = {
    "jev-latest": (None, "jev-latest"),       # None: let the Router pick by language
    "jev-multilingual": ("multilingual", "jev-multilingual"),
    "jev-english": ("english", "jev-english"),
    "jev-fruits": ("fruits-teacher", "jev-fruits"),
}
API_KEY = os.environ.get("LAYA_JEV_API_KEY", "").strip()
MAX_STATE_CHARS = 60_000
MAX_QUESTIONS = 32
MAX_CHOICE_OPTIONS = 255

torch.set_num_threads(int(os.environ.get("LAYA_JEV_THREADS", "4")))

router = Router(models=MODELS, default="multilingual")

app = FastAPI(title="Jev-compatible System One API (Laya backend)")
_inflight = 0
_inflight_lock = Lock()


def _check_auth(authorization: Optional[str]) -> None:
    if not API_KEY:
        return
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="missing or invalid API key")


def _validate(state: Any, questions: Any) -> None:
    if not isinstance(questions, dict) or not questions:
        raise HTTPException(status_code=422, detail="`questions` must be a non-empty object")
    if len(questions) > MAX_QUESTIONS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_QUESTIONS} questions per request")
    if state is None:
        raise HTTPException(status_code=422, detail="`state` is required")
    if not isinstance(state, (str, dict, list)):
        raise HTTPException(status_code=422, detail="`state` must be a string, object or array")
    if isinstance(state, str) and len(state) > MAX_STATE_CHARS:
        raise HTTPException(status_code=422, detail=f"`state` exceeds {MAX_STATE_CHARS} characters")
    for qid, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in ("choice", "score", "noul"):
            raise HTTPException(status_code=422, detail=f"question {qid!r}: `type` must be choice, score or noul")
        if not isinstance(q.get("instructions"), (str, dict, list)):
            raise HTTPException(status_code=422, detail=f"question {qid!r}: `instructions` is required")
        criteria = q.get("criteria")
        if q["type"] in ("choice", "score") and criteria is None:
            raise HTTPException(status_code=422, detail=f"question {qid!r}: `criteria` is required for {q['type']}")
        if q["type"] == "choice" and isinstance(criteria, dict) and len(criteria) > MAX_CHOICE_OPTIONS:
            raise HTTPException(status_code=422, detail=f"question {qid!r}: at most {MAX_CHOICE_OPTIONS} choice options")
        if q["type"] == "score" and isinstance(criteria, list) and len(criteria) > MAX_CHOICE_OPTIONS:
            raise HTTPException(status_code=422, detail=f"question {qid!r}: at most {MAX_CHOICE_OPTIONS} score levels")


def _jev_answer(a: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a laya answer to the Jev wire shape."""
    t = a["type"]
    if t == "noul":
        return {"type": "noul", "noul": round(float(a["noul"]), 4)}
    if t == "choice":
        return {
            "type": "choice",
            "choice": a["choice"],
            "probabilities": {k: round(float(v), 4) for k, v in a["probabilities"].items()},
            "confidence": round(float(a["confidence"]), 4),
        }
    # score
    return {
        "type": "score",
        "score": round(float(a["score"]), 4),
        "legend": a["legend"],
        "probabilities": {k: round(float(v), 4) for k, v in a["probabilities"].items()},
        "confidence": round(float(a["confidence"]), 4),
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "backend": "laya", "models": sorted(MODEL_ALIASES)}


@app.post("/v1/systemone")
async def systemone(request: Request, authorization: Optional[str] = Header(default=None)):
    _check_auth(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be an object")

    model = body.get("model", "jev-latest")
    force, echo = MODEL_ALIASES.get(model, (None, model))
    # A checkpoint name given directly (english / multilingual) also works.
    if force is None and model in MODELS:
        force, echo = model, model
    if force is not None and force not in MODELS:
        raise HTTPException(status_code=422, detail=f"unknown model {model!r}")

    _validate(body.get("state"), body.get("questions"))

    global _inflight
    with _inflight_lock:
        _inflight += 1
    t0 = time.perf_counter()
    try:
        result = router.predict(
            body["state"],
            body["questions"],
            **({"model": force} if force else {}),
        )
    except HTTPException:
        raise
    except Exception as e:  # malformed question payloads surface from inside laya
        raise HTTPException(status_code=422, detail=f"inference failed: {e.__class__.__name__}: {e}")
    finally:
        with _inflight_lock:
            _inflight -= 1

    answers = result["answers"]
    usage_in = int(result.get("usage", {}).get("input_tokens", 0))
    return {
        "model": echo,
        "answers": {qid: _jev_answer(a) for qid, a in answers.items()},
        "usage": {
            "input_tokens": usage_in,
            "output_tokens": sum(1 + len(a.get("probabilities", {})) for a in answers.values()),
        },
    }


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Jev-compatible API (laya backend) on {host}:{port}"
          + ("" if API_KEY else "  [no API key set -- auth disabled]"))
    uvicorn.run(app, host=host, port=port, log_level="info")
