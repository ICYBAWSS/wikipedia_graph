#!/bin/bash
# ============================================================================
# Rebuild viewer_full.bin, titles.bin, edgeTgt.bin, and both directed CSRs for
# the FULL current node set (N~6.9M), reusing the already-computed
# coordinates_rapids.bin positions instead of a new GPU layout run.
#
# No self-termination logic here on purpose, same reasoning as
# runpod_build_csr.sh: several important files come out of this, and I want
# to scp them out and inspect them myself before the pod goes away rather
# than trust an automated "done, delete" step. The externally-armed watchdog
# (this Mac, /tmp/watchdog_<podid>.sh) is the cost ceiling; terminate this pod
# manually once retrieval is confirmed.
#
# Usage (on the pod, after scp'ing coordinates_rapids.bin + rebuild_full_layout.py
# + build_adjacency_csr.py in):
#   ./runpod_full_layout.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a full_layout.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }

echo "=== FULL LAYOUT REBUILD START $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nproc; free -g | head -2; df -h / | tail -1

for f in coordinates_rapids.bin rebuild_full_layout.py; do
  [ -f "$f" ] || { echo "!! $f missing -- scp it in before running"; exit 1; }
done
[ -f ../build_adjacency_csr.py ] || { echo "!! build_adjacency_csr.py missing one level up"; exit 1; }

pip install -q --break-system-packages huggingface_hub numpy || true

echo "downloading test_scrape/wiki_simulation_ctxfix.db (33.4GB, corrected) ..."
hf_cli download "$HF_REPO" test_scrape/wiki_simulation_ctxfix.db --repo-type dataset --local-dir .
mv test_scrape/wiki_simulation_ctxfix.db wiki_simulation_full.db
ls -lh wiki_simulation_full.db coordinates_rapids.bin

python3 rebuild_full_layout.py --db wiki_simulation_full.db --coords coordinates_rapids.bin --out-dir .
touch LAYOUT_DONE

echo "=== COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh viewer_full.bin titles.bin edgeTgt.bin adjacency_csr.bin adjacency_csr_rev.bin
echo "LAYOUT_DONE -- scp all 5 files back, then terminate this pod manually."

echo "cleaning up wiki_simulation_full.db to free disk ..."
rm -f wiki_simulation_full.db
