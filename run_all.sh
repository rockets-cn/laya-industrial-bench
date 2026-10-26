#!/usr/bin/env bash
# Wait for all deps, then run LLM+Laya pipeline bench and full voice pipeline bench.
set -u
cd /home/dfrobot/Work/laya
PY=.venv/bin/python
LLM_MODEL="${LLM_MODEL:-qwen3.5:4b}"

# single-instance lock
if [ -f /tmp/run_all.lock ] && kill -0 "$(cat /tmp/run_all.lock 2>/dev/null)" 2>/dev/null; then
  echo "another run_all is active (pid $(cat /tmp/run_all.lock)); exit"; exit 0
fi
echo $$ > /tmp/run_all.lock

echo "[$(date +%H:%M:%S)] waiting for qwen3.5:4b ..."
while ! ollama list 2>/dev/null | grep -q "$LLM_MODEL"; do sleep 30; done
echo "[$(date +%H:%M:%S)] model ready"

echo "[$(date +%H:%M:%S)] waiting for faster_whisper import ..."
until $PY -c "import faster_whisper" 2>/dev/null; do sleep 20; done
echo "[$(date +%H:%M:%S)] faster_whisper ready"

echo "[$(date +%H:%M:%S)] waiting for whisper-small model file ..."
until [ -f models/whisper-small/model.bin ] && [ $(stat -c%s models/whisper-small/model.bin) -gt 480000000 ]; do sleep 15; done
echo "[$(date +%H:%M:%S)] whisper model ready"

echo "[$(date +%H:%M:%S)] === bench_pipeline (LLM intent + Laya) ==="
$PY bench_pipeline.py 10 3 2>&1 | tee /tmp/pipeline.log
echo "[$(date +%H:%M:%S)] === bench_voice (ASR->LLM->Laya) ==="
HF_ENDPOINT=https://hf-mirror.com $PY bench_voice.py 3 2>&1 | tee /tmp/voice.log
echo "[$(date +%H:%M:%S)] ALL DONE"
