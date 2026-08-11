#!/bin/bash
# ============================================================================
# CONTEXT REBUILD -- recompute link contexts from the pinned enwiki dump using
# the corrected sentence matcher, and publish a slim contexts-only DB to HF.
#
# CPU-only: this is bz2 decompression + regex, no GPU. Run it on a cpu3c pod
# ($0.03/vCPU/hr), never a GPU pod.
#
# SELF-TERMINATING BY DESIGN. The pod kills itself in three independent ways so
# it cannot be left billing if nobody is watching:
#   1. trap EXIT       -- normal finish, error, or Ctrl-C
#   2. hard watchdog   -- separate process, fires at MAX_HOURS even if the main
#                         script wedges or is SIGKILLed
#   3. layered kill    -- runpodctl (pod-scoped key, preinstalled), then the
#                         REST API if RUNPOD_API_KEY is present
# Verify with `runpodctl get pod` afterwards regardless.
#
# Usage (on the pod):
#   HF_TOKEN=hf_xxx ./runpod_contexts.sh
#   SMOKE=20000 HF_TOKEN=hf_xxx ./runpod_contexts.sh   # cheap validation first
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a contexts.log) 2>&1

HF_REPO="${HF_REPO:-icybawss/wikipedia-graph-data}"
DUMP_URL="https://huggingface.co/datasets/icybawss/wikipedia-graph-data/resolve/main/test_scrape/enwiki-latest-pages-articles-multistream.xml.bz2"
STRUCT="wiki_graph_structure.db"
OUT="contexts.db"
SMOKE="${SMOKE:-0}"              # >0 = stop after N articles (validation run)
MAX_HOURS="${MAX_HOURS:-8}"      # hard ceiling; watchdog kills the pod at this
NO_SHUTDOWN="${NO_SHUTDOWN:-0}"  # set 1 only when debugging interactively

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }

# ---- self-destruct ---------------------------------------------------------
terminate_pod() {
    [ "$NO_SHUTDOWN" = "1" ] && { echo "NO_SHUTDOWN=1, staying up"; return; }
    local id="${RUNPOD_POD_ID:-}"
    [ -z "$id" ] && { echo "!! RUNPOD_POD_ID unset -- CANNOT self-terminate, kill it manually"; return; }
    echo "=== terminating pod $id ==="
    runpodctl remove pod "$id"  && return 0
    runpodctl stop   pod "$id"  && return 0
    if [ -n "${RUNPOD_API_KEY:-}" ]; then
        curl -s -X DELETE "https://rest.runpod.io/v1/pods/$id" \
             -H "Authorization: Bearer $RUNPOD_API_KEY" && return 0
    fi
    echo "!! every self-terminate path failed -- kill pod $id manually"
}

upload_results() {
    [ -n "${HF_TOKEN:-}" ] || { echo "no HF_TOKEN, skipping upload"; return; }
    hf_cli upload "$HF_REPO" contexts.log runs/contexts.log --repo-type dataset || true
    if [ -f "$OUT" ] && [ "$SMOKE" = "0" ]; then
        echo "uploading $OUT ($(du -h "$OUT" | cut -f1)) ..."
        hf_cli upload "$HF_REPO" "$OUT" "contexts.db" --repo-type dataset || true
    fi
    echo "exit_code=${1:-?} finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > contexts_done.txt
    hf_cli upload "$HF_REPO" contexts_done.txt runs/contexts_done.txt --repo-type dataset || true
}

on_exit() { rc=$?; set +e; echo "=== EXIT rc=$rc ==="; upload_results "$rc"; terminate_pod; }
trap on_exit EXIT INT TERM

# Watchdog: independent of the main script. If anything wedges -- a stalled
# download, a hung parse -- this still kills the pod. Uses setsid so it survives
# the parent's process group being torn down.
setsid bash -c "sleep $((MAX_HOURS*3600)); echo 'WATCHDOG: MAX_HOURS reached, force terminating';
  runpodctl remove pod '${RUNPOD_POD_ID:-}' || runpodctl stop pod '${RUNPOD_POD_ID:-}' ||
  curl -s -X DELETE 'https://rest.runpod.io/v1/pods/${RUNPOD_POD_ID:-}' -H 'Authorization: Bearer ${RUNPOD_API_KEY:-}'" \
  >> watchdog.log 2>&1 < /dev/null &
echo "watchdog armed: pod dies at +${MAX_HOURS}h no matter what"

# ---- run -------------------------------------------------------------------
echo "=== CONTEXT REBUILD START $(date -u +%Y-%m-%dT%H:%M:%SZ) | smoke=$SMOKE ==="
nproc; free -g | head -2; df -h / | tail -1

pip install -q --break-system-packages huggingface_hub hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1

# Only the structure DB is downloaded (3.1GB, needs random access for the title
# index). The 24.3GB dump is STREAMED and never stored -- a CPU pod's disk caps
# around 30GB, which cannot hold the dump and the ~11GB output at once.
[ -f "$STRUCT" ] || hf_cli download "$HF_REPO" "$STRUCT" --repo-type dataset --local-dir .
ls -lh "$STRUCT"

LIMIT_ARG=""
[ "$SMOKE" != "0" ] && LIMIT_ARG="--limit $SMOKE"
python3 rebuild_contexts.py --dump "$DUMP_URL" --structure-db "$STRUCT" --out "$OUT" $LIMIT_ARG

echo "=== COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh "$OUT"
# trap fires here: uploads, then terminates the pod.
