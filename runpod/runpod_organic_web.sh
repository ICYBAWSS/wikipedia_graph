#!/bin/bash
# ============================================================================
# ORGANIC WEB — recompile with organic (non-disk-seeded) layout so ForceAtlas2
# finds the natural filament/spike structure, then render edge-only monochrome
# webs (tan filaments on #2c2720) to match the reference force-graph aesthetic.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a organic.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
W="${W:-9000}"

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }
heartbeat() { while true; do sleep 150; [ -n "${HF_TOKEN:-}" ] || continue
    { echo "beat=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; tail -n 5 organic.log; } > hb.txt
    hf_cli upload "$HF_REPO" hb.txt runs/full_heartbeat.txt --repo-type dataset >/dev/null 2>&1 || true; done; }
upload_on_exit() {
    rc=$?; [ -n "${HF_TOKEN:-}" ] || return
    hf_cli upload "$HF_REPO" organic.log runs/organic.log --repo-type dataset || true
    hf_cli upload "$HF_REPO" coordinates_organic.bin coordinates_organic.bin --repo-type dataset || true
    for p in org_*.png; do [ -f "$p" ] && hf_cli upload "$HF_REPO" "$p" "smoke/$p" --repo-type dataset || true; done
    echo "exit_code=$rc finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > organic_done.txt
    hf_cli upload "$HF_REPO" organic_done.txt runs/organic_done.txt --repo-type dataset || true
}
heartbeat & HB=$!
trap 'kill $HB 2>/dev/null; upload_on_exit' EXIT

echo "=== ORGANIC WEB START $(date -u +%Y-%m-%dT%H:%M:%SZ) | width=$W ==="
pip install -q datashader matplotlib pillow "huggingface_hub[cli]" hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1

python runpod_check_env.py
[ -f edges_weighted.csv.gz ] || hf_cli download "$HF_REPO" edges_weighted.csv.gz --repo-type dataset --local-dir .
[ -f metadata.csv ]          || hf_cli download "$HF_REPO" metadata.csv          --repo-type dataset --local-dir .

echo "--- Organic recompile (no disk-seeding; FA2 finds web structure) ---"
python compile_galaxy_multistage.py --seed-mode organic --out coordinates_organic.bin --diag diagnostic_organic.png
hf_cli upload "$HF_REPO" diagnostic_organic.png renders/diagnostic_organic.png --repo-type dataset || true

render() { echo ""; echo "--- $1 ---"; python render_galaxy_gpu.py --bin coordinates_organic.bin \
    --width "$W" --height "$W" --output "org_$1.png" $2; }

# Edge-web variants matching the reference (tan filaments on dark brown), different densities
render "web_tan_2m"  "--nodes-off --edge-color '#d8cdb0' --bg-color '#2c2720' --edge-alpha 45 --edge_sample 2000000"
render "web_tan_6m"  "--nodes-off --edge-color '#d8cdb0' --bg-color '#2c2720' --edge-alpha 40 --edge_sample 6000000"
render "web_white_4m" "--nodes-off --edge-color '#e8e8e8' --bg-color '#111111' --edge-alpha 40 --edge_sample 4000000"

echo ""
echo "=== ORGANIC WEB COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh org_*.png
