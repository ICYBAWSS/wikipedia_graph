#!/bin/bash
# ============================================================================
# MERGE contexts.db into a COPY of wiki_simulation.db, upload the copy under a
# NEW filename. The original DB on HF is never touched -- rollback is reverting
# one URL string in engine.js, not restoring a mutated 25GB file.
#
# SELF-TERMINATING, with one important exception: this pod does NOT
# self-terminate if the merged artifact's upload could not be verified. Losing
# the pod means losing the only copy of the merged 33GB file (it's a working
# copy on ephemeral disk, never written back to the original DB), so an
# unverified upload is worth the extra pod-hours to retry/investigate rather
# than silently deleting the only copy. The unconditional MAX_HOURS watchdog
# below is still the hard ceiling regardless -- this only affects the
# "finished successfully" path, not the "something is stuck" path.
#
#   1. trap EXIT/INT/TERM  -- normal finish, error, or Ctrl-C: upload + verify,
#                             terminate ONLY if verified (or if the merge itself
#                             failed, in which case there's nothing to keep the
#                             pod alive for)
#   2. setsid watchdog     -- independent process, fires at MAX_HOURS
#                             unconditionally, even if the main script wedges,
#                             is SIGKILLed, or deliberately stayed up above
#   3. layered kill        -- runpodctl remove -> runpodctl stop -> REST DELETE
#
# POD_ID must be passed explicitly (not assumed from $RUNPOD_POD_ID -- it is
# NOT auto-injected for every pod creation path, confirmed the hard way: a run
# finished and uploaded fine but could not self-terminate because the env var
# was simply unset inside the container, and the pod kept billing until it was
# caught and killed manually from outside).
#
# Usage (on the pod, after scp'ing contexts.db into this dir):
#   POD_ID=abc123 HF_TOKEN=hf_xxx ./runpod_merge.sh
# ============================================================================
set -uo pipefail   # NOT -e: the merge step's own failure must still reach the
                    # trap so upload/terminate logic runs; main() below checks
                    # rc explicitly instead of relying on early-exit.
cd "$(dirname "$0")"
exec > >(tee -a merge.log) 2>&1

HF_REPO="${HF_REPO:-icybawss/wikipedia-graph-data}"
SRC_PATH="test_scrape/wiki_simulation.db"   # never overwritten
WORK="wiki_simulation_work.db"              # local mutable copy
OUT_PATH="test_scrape/wiki_simulation_ctxfix.db"  # new artifact, new name
CONTEXTS="contexts.db"
MAX_HOURS="${MAX_HOURS:-6}"
NO_SHUTDOWN="${NO_SHUTDOWN:-0}"
POD_ID="${POD_ID:-${RUNPOD_POD_ID:-}}"

hf_cli() { if command -v hf >/dev/null 2>&1; then hf "$@"; else huggingface-cli "$@"; fi; }

terminate_pod() {
    [ "$NO_SHUTDOWN" = "1" ] && { echo "NO_SHUTDOWN=1, staying up"; return; }
    [ -z "$POD_ID" ] && { echo "!! POD_ID unset -- CANNOT self-terminate, kill it manually"; return; }
    echo "=== terminating pod $POD_ID ==="
    runpodctl remove pod "$POD_ID" && return 0
    runpodctl stop   pod "$POD_ID" && return 0
    if [ -n "${RUNPOD_API_KEY:-}" ]; then
        curl -s -X DELETE "https://rest.runpod.io/v1/pods/$POD_ID" \
             -H "Authorization: Bearer $RUNPOD_API_KEY" && return 0
    fi
    echo "!! every self-terminate path failed -- kill pod $POD_ID manually"
}

# Upload with retry, then verify via a HEAD request that the remote file's
# size actually matches the local one -- "hf upload" can print success (or
# throw partway through a large-file Xet commit) without the file actually
# landing; a byte-size check against the live repo is the only real proof.
# Prints VERIFIED or UNVERIFIED as its last line so the caller can branch on it
# without parsing anything fragile.
upload_verified() {
    local local_path="$1" remote_path="$2" tries="${3:-4}"
    local expect_size actual_size attempt=1
    expect_size=$(stat -c%s "$local_path" 2>/dev/null || stat -f%z "$local_path")
    while [ "$attempt" -le "$tries" ]; do
        echo "upload attempt $attempt/$tries: $local_path -> $remote_path"
        hf_cli upload "$HF_REPO" "$local_path" "$remote_path" --repo-type dataset
        # -L is required: HF's resolve/main URL 302s to the CDN, and a bare -I
        # (no -L) reads only that redirect's own tiny body length, not the
        # real file's -- silently "verifying" against the wrong response and
        # failing every real upload. Take the LAST content-length in the
        # header stream, since -L prints headers for every hop.
        actual_size=$(curl -sIL "https://huggingface.co/datasets/$HF_REPO/resolve/main/$remote_path" \
                      | grep -i '^content-length:' | tail -1 | tr -d '\r' | awk '{print $2}')
        if [ -n "$actual_size" ] && [ "$actual_size" = "$expect_size" ]; then
            echo "VERIFIED: $remote_path size=$actual_size matches local"
            return 0
        fi
        echo "verification failed (expected=$expect_size actual=${actual_size:-none}), retrying in 30s ..."
        sleep 30
        attempt=$((attempt+1))
    done
    echo "UNVERIFIED: $remote_path after $tries attempts -- NOT deleting the pod"
    return 1
}

on_exit() {
    rc=$?
    set +e
    echo "=== EXIT rc=$rc ==="
    hf_cli upload "$HF_REPO" merge.log runs/merge.log --repo-type dataset || true

    local_ok=1
    if [ -f "$WORK" ] && [ -f MERGE_OK ]; then
        if upload_verified "$WORK" "$OUT_PATH" 4; then
            local_ok=0
        fi
    else
        echo "no MERGE_OK marker -- merge itself did not complete, nothing to upload"
    fi

    echo "exit_code=$rc upload_verified=$([ $local_ok -eq 0 ] && echo yes || echo no) finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > merge_done.txt
    hf_cli upload "$HF_REPO" merge_done.txt runs/merge_done.txt --repo-type dataset || true

    if [ -f MERGE_OK ] && [ "$local_ok" -ne 0 ]; then
        echo "!! merge succeeded but upload could NOT be verified -- staying up for manual recovery."
        echo "!! the $((MAX_HOURS))h watchdog below is still the hard ceiling; this is not an unbounded stay-up."
        return
    fi
    terminate_pod
}
trap on_exit EXIT INT TERM

setsid bash -c "sleep $((MAX_HOURS*3600)); echo 'WATCHDOG: MAX_HOURS reached, force terminating';
  runpodctl remove pod '$POD_ID' || runpodctl stop pod '$POD_ID' ||
  curl -s -X DELETE 'https://rest.runpod.io/v1/pods/$POD_ID' -H 'Authorization: Bearer ${RUNPOD_API_KEY:-}'" \
  >> watchdog.log 2>&1 < /dev/null &
echo "watchdog armed: pod dies at +${MAX_HOURS}h no matter what (pod_id=${POD_ID:-UNSET})"
[ -z "$POD_ID" ] && echo "!! WARNING: POD_ID is empty -- self-termination will not work, only the caller killing it externally will"

echo "=== MERGE START $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nproc; free -g | head -2; df -h / | tail -1

[ -f "$CONTEXTS" ] || { echo "!! $CONTEXTS missing -- scp it in before running"; exit 1; }

pip install -q --break-system-packages huggingface_hub hf_transfer || true
export HF_HUB_ENABLE_HF_TRANSFER=1
# Xet's commit-finalization step is what actually failed last run (raw bytes
# transferred fine at 100%, then new_upload_commit timed out on a 33GB file).
# Force the older, heavily-battle-tested LFS multipart path instead -- slower
# per some benchmarks, but this file only needs to succeed once, not fastest.
export HF_HUB_DISABLE_XET=1

echo "downloading $SRC_PATH (working copy only, original untouched) ..."
hf_cli download "$HF_REPO" "$SRC_PATH" --repo-type dataset --local-dir .
mv "$SRC_PATH" "$WORK"
ls -lh "$WORK" "$CONTEXTS"

python3 merge_contexts.py --main "$WORK" --contexts "$CONTEXTS" && touch MERGE_OK

echo "=== COMPLETE $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
ls -lh "$WORK"
# trap fires here: uploads + verifies the merged copy under $OUT_PATH, then
# terminates the pod ONLY if that verification passed.
