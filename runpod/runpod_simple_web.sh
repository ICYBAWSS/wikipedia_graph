#!/bin/bash
# ============================================================================
# SIMPLE WEB — single-pass ForceAtlas2 (frontend-style) precomputed on GPU,
# then rendered WITH EDGES (the real test of web structure — a position
# scatter can't show it). Colored nodes + white low-opacity edges on #2c2720.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a simple.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
W="${W:-9000}"
SAMPLE="${SAMPLE:-1.0}"
SCALING="${SCALING:-2.0}"
GRAVITY="${GRAVITY:-1.0}"

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }
heartbeat() { while true; do sleep 130; [ -n "${HF_TOKEN:-}" ] || continue
    { echo "beat=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; tail -n 5 simple.log; } > hb.txt
    hf_cli upload "$HF_REPO" hb.txt runs/full_heartbeat.txt --repo-type dataset >/dev/null 2>&1 || true; done; }
upload_on_exit() {
    rc=$?; [ -n "${HF_TOKEN:-}" ] || return
    hf_cli upload "$HF_REPO" simple.log runs/simple.log --repo-type dataset || true
    [ -f coordinates_simple.bin ] && hf_cli upload "$HF_REPO" coordinates_simple.bin coordinates_simple.bin --repo-type dataset || true
    [ -f diag_simple.png ] && hf_cli upload "$HF_REPO" diag_simple.png smoke/diag_simple.png --repo-type dataset || true
    for p in simple_*.png; do [ -f "$p" ] && hf_cli upload "$HF_REPO" "$p" "smoke/$p" --repo-type dataset || true; done
    echo "exit_code=$rc finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > simple_done.txt
    hf_cli upload "$HF_REPO" simple_done.txt runs/simple_done.txt --repo-type dataset || true
}
heartbeat & HB=$!
trap 'kill $HB 2>/dev/null; upload_on_exit' EXIT

echo "=== SIMPLE WEB START $(date -u +%Y-%m-%dT%H:%M:%SZ) | w=$W sample=$SAMPLE scaling=$SCALING gravity=$GRAVITY ==="
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1
python runpod_check_env.py
[ -f edges_weighted.csv.gz ] || hf_cli download "$HF_REPO" edges_weighted.csv.gz --repo-type dataset --local-dir .
[ -f metadata.csv ]          || hf_cli download "$HF_REPO" metadata.csv          --repo-type dataset --local-dir .

echo "--- Single-pass FA2 compile ---"
python compile_galaxy_multistage.py --layout simple --sample-frac "$SAMPLE" \
    --fa2-scaling "$SCALING" --fa2-gravity "$GRAVITY" \
    --out coordinates_simple.bin --diag diag_simple.png

render() { echo ""; echo "--- $1 ---"; python render_galaxy_gpu.py --bin coordinates_simple.bin \
    --width "$W" --height "$W" --output "simple_$1.png" $2; }

# The real web test: edges are the web. White low-opacity on brown + colored nodes.
render "web_edges_only" "--nodes-off --bg-color #2c2720 --edge-alpha 45 --edge_sample 8000000"
render "web_community"  "--color-by community --bg-color #2c2720 --edge-alpha 35 --edge_sample 8000000"

echo ""
echo "=== SIMPLE WEB COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh simple_*.png coordinates_simple.bin
