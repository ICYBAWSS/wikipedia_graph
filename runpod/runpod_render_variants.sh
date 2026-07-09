#!/bin/bash
# ============================================================================
# RENDER VARIANTS — re-render the ALREADY-COMPUTED layout several ways to pick
# an edge/aesthetic treatment. No recompile: pulls coordinates_rapids.bin from HF.
#
# Renders at 8000px (fast, enough to judge) and uploads each to smoke/variant_*.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a variants.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
W="${W:-8000}"

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }

upload_on_exit() {
    rc=$?
    [ -n "${HF_TOKEN:-}" ] || return
    hf_cli upload "$HF_REPO" variants.log runs/variants.log --repo-type dataset || true
    for p in variant_*.png; do [ -f "$p" ] && hf_cli upload "$HF_REPO" "$p" "smoke/$p" --repo-type dataset || true; done
    echo "exit_code=$rc finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > variants_done.txt
    hf_cli upload "$HF_REPO" variants_done.txt runs/variants_done.txt --repo-type dataset || true
}
heartbeat() { while true; do sleep 120; [ -n "${HF_TOKEN:-}" ] || continue
    { echo "beat=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; tail -n 4 variants.log; } > hb.txt
    hf_cli upload "$HF_REPO" hb.txt runs/full_heartbeat.txt --repo-type dataset >/dev/null 2>&1 || true; done; }
heartbeat & HB=$!
trap 'kill $HB 2>/dev/null; upload_on_exit' EXIT

echo "=== VARIANTS START $(date -u +%Y-%m-%dT%H:%M:%SZ) | width=$W ==="
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1

python runpod_check_env.py

echo "--- Fetching precomputed coordinates + edges ---"
[ -f coordinates_rapids.bin ] || hf_cli download "$HF_REPO" coordinates_rapids.bin --repo-type dataset --local-dir .
[ -f edges_weighted.csv.gz ]  || hf_cli download "$HF_REPO" edges_weighted.csv.gz  --repo-type dataset --local-dir .

# name | extra args
render() {
    echo ""; echo "--- Variant: $1 ---"
    python render_galaxy_gpu.py --bin coordinates_rapids.bin --width "$W" --height "$W" \
        --output "variant_$1.png" $2
}

# New wheel-spread palette. Test plain vs Biography-demoted vs strong-demote.
render "palette_plain"   "--edges-off"
render "demote_bio_04"   "--edges-off --cat-weights 0:0.4"
render "demote_bio_025"  "--edges-off --cat-weights 0:0.25"
render "demote_edges"    "--edge-alpha 25 --cat-weights 0:0.4"

echo ""
echo "=== VARIANTS COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh variant_*.png
