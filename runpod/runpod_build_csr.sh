#!/bin/bash
# ============================================================================
# Rebuild adjacency_csr.bin from the corrected wiki_simulation_ctxfix.db.
#
# Lighter than the merge job: read-only sequential scan + numpy, no SQL writes,
# no VACUUM, no upload (the output is a small local file this project fetches
# via a plain relative path -- see engine.js's fetch('adjacency_csr.bin') --
# not something hosted on HF, so it just needs to come back to this Mac via scp).
#
# No self-termination logic here on purpose: the file is small and I want to
# scp it out and inspect it myself before the pod goes away, rather than trust
# an automated "done, delete" step for what's now the third pod in this chain.
# The externally-armed watchdog (this Mac, /tmp/watchdog_<podid>.sh) is the
# cost ceiling; I terminate this pod manually once the retrieval is confirmed.
#
# Usage (on the pod, after scp'ing viewer_full.bin + build_adjacency_csr.py in):
#   ./runpod_build_csr.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a csr_build.log) 2>&1

HF_REPO="icybawss/wikipedia-graph-data"
hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }

echo "=== CSR BUILD START $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nproc; free -g | head -2; df -h / | tail -1

for f in viewer_full.bin build_adjacency_csr.py; do
  [ -f "$f" ] || { echo "!! $f missing -- scp it in before running"; exit 1; }
done

pip install -q --break-system-packages huggingface_hub numpy || true

echo "downloading test_scrape/wiki_simulation_ctxfix.db (33.4GB, corrected) ..."
hf_cli download "$HF_REPO" test_scrape/wiki_simulation_ctxfix.db --repo-type dataset --local-dir .
mv test_scrape/wiki_simulation_ctxfix.db wiki_simulation_full.db
ls -lh wiki_simulation_full.db viewer_full.bin

python3 build_adjacency_csr.py
touch CSR_DONE

echo "=== COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh adjacency_csr.bin adjacency_csr_rev.bin
echo "CSR_DONE -- scp adjacency_csr.bin AND adjacency_csr_rev.bin back, then terminate this pod manually."
