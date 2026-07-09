#!/bin/bash
# ============================================================================
# WEB RUN — recompile the layout (now emitting per-node Louvain community IDs)
# then render several community-colored + edge treatments to dial in a
# cosmic-web look. Uploads the new 6-column coordinates_rapids.bin + variants.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a web_run.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
EWI3="${EWI3:-0.4}"
W="${W:-9000}"

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }
heartbeat() { while true; do sleep 150; [ -n "${HF_TOKEN:-}" ] || continue
    { echo "beat=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; tail -n 5 web_run.log; } > hb.txt
    hf_cli upload "$HF_REPO" hb.txt runs/full_heartbeat.txt --repo-type dataset >/dev/null 2>&1 || true; done; }
upload_on_exit() {
    rc=$?; [ -n "${HF_TOKEN:-}" ] || return
    hf_cli upload "$HF_REPO" web_run.log runs/web_run.log --repo-type dataset || true
    for p in web_*.png; do [ -f "$p" ] && hf_cli upload "$HF_REPO" "$p" "smoke/$p" --repo-type dataset || true; done
    echo "exit_code=$rc finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > web_done.txt
    hf_cli upload "$HF_REPO" web_done.txt runs/web_done.txt --repo-type dataset || true
}
heartbeat & HB=$!
trap 'kill $HB 2>/dev/null; upload_on_exit' EXIT

echo "=== WEB RUN START $(date -u +%Y-%m-%dT%H:%M:%SZ) | ewi3=$EWI3 width=$W ==="
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1

python runpod_check_env.py

echo "--- Fetching inputs ---"
[ -f edges_weighted.csv.gz ] || hf_cli download "$HF_REPO" edges_weighted.csv.gz --repo-type dataset --local-dir .
[ -f metadata.csv ]          || hf_cli download "$HF_REPO" metadata.csv          --repo-type dataset --local-dir .

echo "--- Recompile (emits community column) ---"
python compile_galaxy_multistage.py --ewi3 "$EWI3" --out coordinates_rapids.bin --diag diagnostic_layout.png
# bank the new 6-col layout immediately
hf_cli upload "$HF_REPO" coordinates_rapids.bin coordinates_rapids.bin --repo-type dataset || true
hf_cli upload "$HF_REPO" diagnostic_layout.png renders/diagnostic_layout.png --repo-type dataset || true

render() { echo ""; echo "--- $1 ---"; python render_galaxy_gpu.py --bin coordinates_rapids.bin \
    --width "$W" --height "$W" --output "web_$1.png" $2; }

render "community_noedge"  "--color-by community --edges-off"
render "community_web40"   "--color-by community --edge-alpha 40"
render "community_web75"   "--color-by community --edge-alpha 75"
render "category_web40"    "--color-by category --edge-alpha 40"

echo ""
echo "=== WEB RUN COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh web_*.png
