#!/bin/bash
# ============================================================================
# UMAP LAYOUT — structural embedding instead of ForceAtlas2.
# FA2 has two failure modes on Wikipedia (scale-free): the "organic/simple"
# regime hairballs into a uniform blob, and the "communities" regime only
# separates by IMPOSING geometry (golden-spiral rings + overlap annuli) which
# reads as artificial rings. UMAP has neither: positions emerge from neighbor
# similarity, min_dist floors the spacing so hubs can't collapse.
#
# Validates on a small edge SAMPLE first (same as smoke_test) — rendered WITH
# EDGES, because a bare position scatter can't show ring/web structure.
# Sweeps two min_dist values so you can compare tight vs. loose in one run.
#
# Usage:  ./runpod_umap.sh
#   SAMPLE=0.03 NEIGH=15 MINDISTS="0.1 0.5" W=4000 ./runpod_umap.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a umap.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
SAMPLE="${SAMPLE:-0.03}"      # edge subsample (1.0 = full 8M run)
NEIGH="${NEIGH:-15}"          # UMAP n_neighbors
MINDISTS="${MINDISTS:-0.1 0.5}"  # sweep: tight vs loose spacing
W="${W:-4000}"               # render size

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }
heartbeat() { while true; do sleep 130; [ -n "${HF_TOKEN:-}" ] || continue
    { echo "beat=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; tail -n 5 umap.log; } > hb.txt
    hf_cli upload "$HF_REPO" hb.txt runs/umap_heartbeat.txt --repo-type dataset >/dev/null 2>&1 || true; done; }
upload_on_exit() {
    rc=$?; [ -n "${HF_TOKEN:-}" ] || return
    hf_cli upload "$HF_REPO" umap.log runs/umap.log --repo-type dataset || true
    for p in umap_*.png diag_umap_*.png; do [ -f "$p" ] && hf_cli upload "$HF_REPO" "$p" "smoke/$p" --repo-type dataset || true; done
    echo "exit_code=$rc finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > umap_done.txt
    hf_cli upload "$HF_REPO" umap_done.txt runs/umap_done.txt --repo-type dataset || true
}
heartbeat & HB=$!
trap 'kill $HB 2>/dev/null; upload_on_exit' EXIT

echo "=== UMAP START $(date -u +%Y-%m-%dT%H:%M:%SZ) | sample=$SAMPLE neigh=$NEIGH mindists='$MINDISTS' w=$W ==="
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv || true
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1
python -c "from cuml.manifold import UMAP; print('cuml UMAP OK')"

[ -f edges_weighted.csv.gz ] || hf_cli download "$HF_REPO" edges_weighted.csv.gz --repo-type dataset --local-dir .
[ -f metadata.csv ]          || hf_cli download "$HF_REPO" metadata.csv          --repo-type dataset --local-dir .

for MD in $MINDISTS; do
    tag="n${NEIGH}_d${MD}"
    echo ""; echo "--- UMAP compile: n_neighbors=$NEIGH min_dist=$MD ---"
    python compile_galaxy_multistage.py --layout umap --sample-frac "$SAMPLE" \
        --umap-neighbors "$NEIGH" --umap-min-dist "$MD" \
        --out "coords_umap_${tag}.bin" --diag "diag_umap_${tag}.png"

    echo "--- render edges-only (web/ring test) ---"
    python render_galaxy_gpu.py --bin "coords_umap_${tag}.bin" \
        --width "$W" --height "$W" --output "umap_${tag}_edges.png" \
        --nodes-off --bg-color '#2c2720' --edge-alpha 45 --edge_sample 2000000
    echo "--- render community coloring ---"
    python render_galaxy_gpu.py --bin "coords_umap_${tag}.bin" \
        --width "$W" --height "$W" --output "umap_${tag}_community.png" \
        --color-by community --bg-color '#2c2720' --edge-alpha 35 --edge_sample 2000000
done

echo ""; echo "=== UMAP COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh umap_*.png diag_umap_*.png coords_umap_*.bin 2>/dev/null || true
echo "Compare diag_umap_*.png (raw scatter) + umap_*_edges.png (web). No rings, no blob = win."
echo "Then run full 8M:  SAMPLE=1.0 MINDISTS=<winner> ./runpod_umap.sh"
