# Fine-tuning a local model for roadwatch

A LoRA adapter trained on the app's own task, fused into Qwen3-14B, served locally
and picked from the same model dropdown as the cloud providers. Free, private,
and specialized: the training signal is filtered through `citation_valid`, so the
model learns to quote evidence verbatim instead of fabricating plausible citations.

## Pipeline

```bash
# 0. Dev-only dependency (not part of the roadwatch install)
.venv/bin/pip install mlx-lm

# 1. Training data: distilled teacher runs (validator-filtered) + exemplars.
#    ~2 teacher calls per project; edit --runs/--max-runs to taste.
.venv/bin/python finetune/distill.py --teacher default --out finetune/data
.venv/bin/python finetune/distill.py --exemplars-only --out finetune/data   # offline check

# 2. Train the adapter (hours on an M-series; watch finetune/adapters/)
.venv/bin/python -m mlx_lm.lora -c finetune/lora.yaml

# 3. Fuse adapters into the 4-bit base and serve on http://127.0.0.1:8080
./finetune/serve.sh

# 4. Point the app at it (a second provider choice alongside Ollama/cloud):
#    ROADWATCH_LOCAL_LABEL=Roadwatch Qwen3-14B (fine-tuned, this Mac)
#    ROADWATCH_LOCAL_MODEL=roadwatch-qwen3-14b
#    ROADWATCH_LOCAL_BASE_URL=http://127.0.0.1:8080/v1
#    ROADWATCH_LOCAL_API_KEY=local
#    ROADWATCH_LOCAL_TIMEOUT=300

# 5. Prove it instead of asserting it: same cases, every provider
.venv/bin/python finetune/eval.py local default nvidia_lightning
```

## What the model is actually trained to do

`distill.py` captures the *exact* prompt roadwatch sends (system contract +
context JSON) and pairs it with the *exact* output the app accepts: the report
after `parse_report` + `citation_valid` filtering. Runs that produce nothing
verifiable are dropped. So the adapter concentrates on three behaviours:

1. **The JSON contract** — findings/answers/missing_records in the app's schema.
2. **Citation discipline** — quotes that exist in the evidence, on the cited page.
3. **Honest abstention** — `missing_records` instead of invented findings; the
   hand-written exemplars also cover prompt-injection in evidence and the
   confidence ladder (supported / plausible / speculative).

The trained model answers the report directly from the context (dossiers +
checks); the tool loop still works, it just usually isn't needed — the final
JSON is returned on the first content turn.

## Sizing notes (24 GB)

- Base `Qwen3-14B` 4-bit ≈ 9 GB — trains and serves with context headroom.
- Do **not** fuse with `--de-quantize` (~28 GB, won't fit).
- 800 iters × batch 1 × 8k context ≈ a few hours; `iters`/`num_layers` in
  `lora.yaml` are the levers if memory or time runs short.
- Data quality gates quantity: ~20 clean distilled runs beat 200 sloppy ones.
