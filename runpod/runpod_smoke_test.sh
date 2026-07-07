#!/bin/bash
# ============================================================================
# RunPod SMOKE TEST — validate the full layout+render pipeline cheaply
# before committing to the multi-hour 8M-node run.
#
# What it does:
#   1. Preflight: verify cugraph/cudf/datashader APIs on a tiny graph
#   2. Download edges_weighted.csv.gz + metadata.csv from HF (if missing)
#   3. Compile layout TWICE on a 3% edge sample at 20% iterations:
#        A) Phase 3 edge_weight_influence = 0.4  (current baseline)
#        B) Phase 3 edge_weight_influence = 0.0  (pure-topology blossom)
#   4. Render both to 4000px PNGs
#
# Compare the two smoke_render_*.png / diagnostic_sample_*.png pairs,
# pick the ewi that looks better, then run:  EWI3=<winner> ./runpod_full_run.sh
#
# Usage:  ./runpod_smoke_test.sh          (all output tee'd to smoke_test.log)
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a smoke_test.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
SAMPLE_FRAC="${SAMPLE_FRAC:-0.03}"
ITERS_SCALE="${ITERS_SCALE:-0.2}"

echo "=== SMOKE TEST START $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv || true

echo ""
echo "--- Step 0: Installing render/CLI deps (no-op if present) ---"
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1   # saturate datacenter bandwidth on the 1.25GB edge file

# hf (new CLI) with huggingface-cli fallback
hf_download() {
    local file="$1"
    if command -v hf >/dev/null 2>&1; then
        hf download "$HF_REPO" "$file" --repo-type dataset --local-dir .
    else
        huggingface-cli download "$HF_REPO" "$file" --repo-type dataset --local-dir .
    fi
}

echo ""
echo "--- Step 1: Environment preflight ---"
python runpod_check_env.py

echo ""
echo "--- Step 2: Fetching input data from HF ($HF_REPO) ---"
[ -f edges_weighted.csv.gz ] || hf_download edges_weighted.csv.gz
[ -f metadata.csv ]          || hf_download metadata.csv
ls -lh edges_weighted.csv.gz metadata.csv

echo ""
echo "--- Step 3A: Sampled compile, Phase 3 ewi=0.4 (baseline) ---"
python compile_galaxy_multistage.py \
    --sample-frac "$SAMPLE_FRAC" --iters-scale "$ITERS_SCALE" \
    --ewi3 0.4 --out sample_ewi04.bin --diag diagnostic_sample_ewi04.png

echo ""
echo "--- Step 3B: Sampled compile, Phase 3 ewi=0.0 (pure topology) ---"
python compile_galaxy_multistage.py \
    --sample-frac "$SAMPLE_FRAC" --iters-scale "$ITERS_SCALE" \
    --ewi3 0.0 --out sample_ewi00.bin --diag diagnostic_sample_ewi00.png

echo ""
echo "--- Step 4: Rendering both variants at 4000px ---"
python render_galaxy_gpu.py --bin sample_ewi04.bin \
    --width 4000 --height 4000 --edge_sample 2000000 --output smoke_render_ewi04.png
python render_galaxy_gpu.py --bin sample_ewi00.bin \
    --width 4000 --height 4000 --edge_sample 2000000 --output smoke_render_ewi00.png

echo ""
echo "=== SMOKE TEST COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Artifacts:"
ls -lh diagnostic_sample_ewi*.png smoke_render_ewi*.png sample_ewi*.bin 2>/dev/null || true
echo ""
echo "Next: compare the two renders, then launch the full run with the winner:"
echo "  EWI3=0.4 ./runpod_full_run.sh    # or EWI3=0.0"
