"""
Build compact DIRECTED adjacency structures (CSR: offsets + flat neighbor list)
for in-memory client-side pathfinding, restricted to the RunPod-computed layout's
node set so every index in the output has a valid position in viewer_full.bin.

Two separate directed structures, not one undirected one:
  adjacency_csr.bin       OUT-edges: neighbors of i are articles i links TO.
  adjacency_csr_rev.bin   IN-edges:  neighbors of i are articles that link TO i.

Earlier version merged both directions into one undirected structure ("each edge
appears from both endpoints"). That let route pathfinding treat an in-link as if
it were an out-link -- a route hop reconstructed from that graph could show
A -> B where the only real wikitext link is B -> [[A]], which a person reading
article A could never actually click through, since A's page has no link to B.
Bidirectional BFS needs both directions for different reasons (the *forward*
search from the start must only ever follow real out-links; the *backward*
search from the target must ask "who links to nodes near the target," i.e.
walk in-links, backwards, to also assemble a real out-link chain once the two
frontiers meet) -- see engine.js's runBidirectionalBFS. Neither is "no
direction," which is what the old single merged structure amounted to.

Source: wiki_simulation_full.db, a local copy of test_scrape/wiki_simulation_ctxfix.db
from the icybawss/wikipedia-graph-data HF dataset — the context-corrected DB the app
now queries live over HTTP (see scraper/xml_parser.py's rewritten
parse_wikitext_links + runpod/merge_contexts.py). Reading it locally means this CSR
has zero drift from the live links table (earlier attempts using the smaller
wiki_graph_structure.db companion export were confirmed missing a handful of edges
present here, e.g. 4->0).

File format (both files, same layout):
  uint32 N                  node count
  uint32 E                  total directed-entry count (== len(neighbors))
  uint32[N+1] offsets       neighbors of node i are neighbors[offsets[i]:offsets[i+1]]
  uint32[E]   neighbors     flat, single direction only

build_csr() is also imported directly by runpod/rebuild_full_layout.py, which
already has src/tgt arrays in memory from its own single pass over `links` and
reuses this rather than re-deriving the same dedup/CSR-offset logic. Everything
below main() only runs when this file is executed directly (`python
build_adjacency_csr.py`), not on import.
"""
import struct
import time
import numpy as np
import sqlite3
import os
import sys
import urllib.request

SRC_DB = "wiki_simulation_full.db"
VIEWER_BIN = "viewer_full.bin"
OUT_PATH = "adjacency_csr.bin"
OUT_PATH_REV = "adjacency_csr_rev.bin"


def download_progress(block_num, block_size, total_size):
    downloaded = block_num * block_size
    percent = min(100, (downloaded / total_size) * 100)
    sys.stdout.write(f"\rDownloading database: {percent:.2f}% ({downloaded / 1e9:.2f} GB / {total_size / 1e9:.2f} GB)")
    sys.stdout.flush()


def build_csr(row_idx, col_idx, out_path, label, N):
    """Dedupe (row_idx[i], col_idx[i]) pairs and write a directed CSR: neighbors
    of `row` are the deduped `col` values for that row. Caller controls direction
    entirely via which array is passed as row_idx vs col_idx -- pass (src, tgt) for
    an out-edge (forward) structure, (tgt, src) for an in-edge (reverse) one.
    """
    t0 = time.time()
    print(f"Deduplicating and sorting ({label}) ...")
    key = row_idx.astype(np.int64) * N + col_idx.astype(np.int64)
    key = np.unique(key)  # dedupes AND sorts ascending -> already CSR row order

    nbr_row = (key // N).astype(np.uint32)
    nbr_col = (key % N).astype(np.uint32)
    del key
    E = len(nbr_col)
    print(f"  {E:,} directed entries after dedup ({time.time()-t0:.1f}s elapsed)")

    print("  Building CSR offsets ...")
    counts = np.bincount(nbr_row, minlength=N)
    offsets = np.zeros(N + 1, dtype=np.uint32)
    np.cumsum(counts, out=offsets[1:])
    assert offsets[-1] == E, f"offset/edge count mismatch: {offsets[-1]} vs {E}"
    del counts, nbr_row

    max_deg = int(np.diff(offsets).max())
    print(f"  max degree = {max_deg:,}, avg degree = {E/N:.1f}")

    print(f"  Writing {out_path} ...")
    with open(out_path, "wb") as f:
        f.write(struct.pack("<II", N, E))
        f.write(offsets.tobytes())
        f.write(nbr_col.tobytes())

    size_mb = os.path.getsize(out_path) / 1e6
    print(f"  Done. {out_path}: {size_mb:.1f} MB")


def main():
    if not os.path.exists(SRC_DB):
        url = "https://huggingface.co/datasets/icybawss/wikipedia-graph-data/resolve/main/test_scrape/wiki_simulation_ctxfix.db"
        print(f"{SRC_DB} not found locally.")
        print("Downloading 33.4 GB corrected database from Hugging Face. This may take a few minutes...")
        urllib.request.urlretrieve(url, SRC_DB, download_progress)
        print("\nDownload complete!")

    t0 = time.time()

    with open(VIEWER_BIN, "rb") as f:
        N = struct.unpack("<I", f.read(4))[0]
    print(f"Layout N (node count) = {N:,}")

    print("Reading all links from wiki_simulation_full.db ...")
    # No WHERE clause on source_idx/target_idx: `links` is stored in rowid order, so
    # this is a fast sequential scan. Filtering source_idx/target_idx < N here (even as
    # a broad, non-selective range) makes SQLite pick the target_idx index and do
    # ~150M+ random single-row lookups back into the table — dramatically slower than
    # reading everything sequentially and filtering with numpy afterward, done below.
    con = sqlite3.connect(SRC_DB)
    con.execute("PRAGMA query_only = 1")
    cur = con.execute("SELECT source_idx, target_idx FROM links WHERE context IS NOT NULL AND context != ''")

    # Pull in chunks to keep peak memory bounded and give visible progress.
    src_chunks, tgt_chunks = [], []
    CHUNK = 5_000_000
    total = 0
    while True:
        rows = cur.fetchmany(CHUNK)
        if not rows:
            break
        arr = np.array(rows, dtype=np.int32)
        src_chunks.append(arr[:, 0])
        tgt_chunks.append(arr[:, 1])
        total += len(rows)
        print(f"  {total:,} rows read ({time.time()-t0:.1f}s elapsed)")
    con.close()

    src_all = np.concatenate(src_chunks)
    tgt_all = np.concatenate(tgt_chunks)
    del src_chunks, tgt_chunks
    print(f"Total directed edges read: {len(src_all):,}")

    in_range = (src_all < N) & (tgt_all < N)
    src = src_all[in_range]
    tgt = tgt_all[in_range]
    del src_all, tgt_all, in_range

    self_loop = (src == tgt)  # not clickable-useful (a page "linking to itself" as a hop)
    src = src[~self_loop]
    tgt = tgt[~self_loop]
    print(f"In-range, non-self-loop directed edges: {len(src):,}")

    # Forward (out-edges): neighbors of i are articles i links to. Drives the
    # forward half of bidirectional BFS and the whole of the greedy pathfinder.
    build_csr(src, tgt, OUT_PATH, "forward/out", N)

    # Reverse (in-edges): neighbors of i are articles that link to i. Drives the
    # backward half of bidirectional BFS -- growing "who can reach the target"
    # requires walking in-links, not out-links.
    build_csr(tgt, src, OUT_PATH_REV, "reverse/in", N)

    print(f"Total time: {time.time()-t0:.1f}s")

    if os.path.exists(SRC_DB):
        print(f"Cleaning up {SRC_DB} to free disk space...")
        os.remove(SRC_DB)
        print("Cleanup complete!")


if __name__ == "__main__":
    main()
