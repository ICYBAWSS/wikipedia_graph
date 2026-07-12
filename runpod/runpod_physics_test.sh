#!/bin/bash
# ============================================================================
# PHYSICS TEST — cheap validation of the organic web physics on a 15% edge
# sample. Just produces diagnostic scatters (web vs ring vs blob) so we can
# tune FA2 physics for pennies before committing to a full run.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a physics.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
SAMPLE="${SAMPLE:-0.15}"

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }
heartbeat() { while true; do sleep 120; [ -n "${HF_TOKEN:-}" ] || continue
    { echo "beat=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; tail -n 4 physics.log; } > hb.txt
    hf_cli upload "$HF_REPO" hb.txt runs/full_heartbeat.txt --repo-type dataset >/dev/null 2>&1 || true; done; }
upload_on_exit() {
    rc=$?; [ -n "${HF_TOKEN:-}" ] || return
    hf_cli upload "$HF_REPO" physics.log runs/physics.log --repo-type dataset || true
    for p in diag_phys_*.png; do [ -f "$p" ] && hf_cli upload "$HF_REPO" "$p" "smoke/$p" --repo-type dataset || true; done
    echo "exit_code=$rc finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > physics_done.txt
    hf_cli upload "$HF_REPO" physics_done.txt runs/physics_done.txt --repo-type dataset || true
}
heartbeat & HB=$!
trap 'kill $HB 2>/dev/null; upload_on_exit' EXIT

echo "=== PHYSICS TEST START $(date -u +%Y-%m-%dT%H:%M:%SZ) | sample=$SAMPLE ==="
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1
python runpod_check_env.py
[ -f edges_weighted.csv.gz ] || hf_cli download "$HF_REPO" edges_weighted.csv.gz --repo-type dataset --local-dir .
[ -f metadata.csv ]          || hf_cli download "$HF_REPO" metadata.csv          --repo-type dataset --local-dir .

# Organic web physics on a sample — just the diagnostic scatter (fast)
echo "--- Organic web physics (sample $SAMPLE) ---"
python compile_galaxy_multistage.py --seed-mode organic --sample-frac "$SAMPLE" \
    --out sample_organic.bin --diag diag_phys_organic.png

echo ""
echo "=== PHYSICS TEST COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh diag_phys_*.png
