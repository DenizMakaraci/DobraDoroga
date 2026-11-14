#!/bin/sh
# Fuse the trained adapters into the base model and serve it locally (OpenAI-compatible).
# Requires: pip install mlx-lm   (dev-only, not a roadwatch dependency)
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python

BASE="${1:-mlx-community/Qwen3-14B-MLX-4bit}"
OUT=finetune/roadwatch-qwen3-14b

# Merge adapters into the 4-bit weights (stays quantized - do NOT pass --de-quantize,
# the de-quantized model is ~28 GB and will not fit 24 GB).
$PY -m mlx_lm.fuse --model "$BASE" --adapter-path finetune/adapters --save-path "$OUT"

# OpenAI-compatible server on :8080. Qwen3 gets thinking disabled by ai.model_options.
exec $PY -m mlx_lm.server --model "$OUT" --host 127.0.0.1 --port 8080
