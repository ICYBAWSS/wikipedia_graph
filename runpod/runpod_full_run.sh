#!/bin/bash
# ============================================================================
# RunPod FULL RUN — production 8M-node layout compile + 16k render.
#
# Run the smoke test FIRST:  ./runpod_smoke_test.sh
#
# Usage:
#   EWI3=0.4 ./runpod_full_run.sh          # ewi winner from the smoke A/B (default 0.4)
#   UPLOAD=1 HF_TOKEN=hf_xxx EWI3=0.0 ./runpod_full_run.sh   # also upload artifacts to HF
#
# Outputs:
#   coordinates_rapids.bin    — layout binary (consumed by the WebGL visualizer)
#   diagnostic_layout.png     — 50k-node scatter sanity check
#   massive_galaxy_full.png   — 16000px final render
#   full_run.log              — complete log
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a full_run.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
EWI3="${EWI3:-0.4}"
WIDTH="${WIDTH:-16000}"
UPLOAD="${UPLOAD:-0}"

echo "=== FULL RUN START $(date -u +%Y-%m-%dT%H:%M:%SZ) | ewi3=$EWI3 width=$WIDTH ==="
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv || true

echo ""
echo "--- Step 0: Installing render/CLI deps (no-op if present) ---"
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1   # saturate datacenter bandwidth on the 1.25GB edge file

hf_cli() {
    if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi
}

# Push log + done-marker to HF on exit (success OR crash) for remote monitoring
upload_run_log() {
    rc=$?
    if [ -n "${HF_TOKEN:-}" ]; then
        hf_cli upload "$HF_REPO" full_run.log runs/full_run.log --repo-type dataset || true
        echo "exit_code=$rc finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > full_done.txt
        hf_cli upload "$HF_REPO" full_done.txt runs/full_done.txt --repo-type dataset || true
    fi
}
trap upload_run_log EXIT

echo ""
echo "--- Step 1: Environment preflight ---"
python runpod_check_env.py

echo ""
echo "--- Step 2: Fetching input data from HF ($HF_REPO) ---"
[ -f edges_weighted.csv.gz ] || hf_cli download "$HF_REPO" edges_weighted.csv.gz --repo-type dataset --local-dir .
[ -f metadata.csv ]          || hf_cli download "$HF_REPO" metadata.csv          --repo-type dataset --local-dir .
ls -lh edges_weighted.csv.gz metadata.csv

echo ""
echo "--- Step 3: FULL multi-stage layout compile (this is the long one) ---"
python compile_galaxy_multistage.py \
    --ewi3 "$EWI3" --out coordinates_rapids.bin --diag diagnostic_layout.png

echo ""
echo "--- Step 4: 16k GPU render ---"
python render_galaxy_gpu.py --bin coordinates_rapids.bin \
    --width "$WIDTH" --height "$WIDTH" --output massive_galaxy_full.png

echo ""
if [ "$UPLOAD" = "1" ]; then
    echo "--- Step 5: Uploading artifacts to HF ($HF_REPO) ---"
    hf_cli upload "$HF_REPO" coordinates_rapids.bin coordinates_rapids.bin --repo-type dataset
    hf_cli upload "$HF_REPO" diagnostic_layout.png renders/diagnostic_layout.png --repo-type dataset
    hf_cli upload "$HF_REPO" massive_galaxy_full.png renders/massive_galaxy_full.png --repo-type dataset
else
    echo "--- Step 5: Skipping upload (set UPLOAD=1 HF_TOKEN=... to enable) ---"
fi

echo ""
echo "=== FULL RUN COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh coordinates_rapids.bin diagnostic_layout.png massive_galaxy_full.png 2>/dev/null || true
