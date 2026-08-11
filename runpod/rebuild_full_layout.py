"""
Rebuild viewer_full.bin, titles.bin, edgeTgt.bin, and both directed CSRs for the
FULL current node set (N=6,919,752), instead of the stale 5,483,618-node subset.

WHY: the deployed layout was frozen from a June 22 snapshot; the live database
has since grown by ~1.44M articles (including e.g. "United States") that were
never added to the layout, titles index, or CSR -- confirmed missing from
search, unplaceable on the map, unreachable by pathfinding.

WHY NO NEW GPU JOB: coordinates_rapids.bin (the already-computed FA2 layout,
"already established settings" -- multistage community pipeline, ewi3=0.4,
spacing=2200, seed-disk=1.3, min-ring=6, see runpod_web_run.sh) already has a
valid, non-degenerate position for all 6,919,752 nodes, verified directly
(spot-checked "United States" and 138 nodes spread across the full range: zero
degenerate/NaN positions). Its own per-node degree/category columns are stale
(spot-checked against live data and found unreliable, same class of issue as
the views corruption already found and removed from the UI) so this recomputes
degree/category fresh from the live DB rather than trusting them, and only
reuses coordinates_rapids.bin for x/y.

Single sequential pass over `links` (same context-filtered query
build_adjacency_csr.py already uses, for consistency with what pathfinding and
the sidebar can actually reach) drives THREE outputs at once instead of three
separate scans:
  - degree per node (both directions) -> viewer_full.bin
  - each node's single highest-degree neighbor -> edgeTgt.bin (ambient
    background hairline rendering; purely cosmetic, not used for pathfinding)
  - the two directed adjacency lists -> handed to build_adjacency_csr.py's
    existing, already-verified build_csr() function (imported, not
    reimplemented, so the direction-correctness work already done and tested
    this session isn't duplicated or risked drifting out of sync)

Output formats match exactly what engine.js already parses -- unchanged reader
code, only the underlying N and data grow:
  viewer_full.bin  uint32 N, then N*4 float32 (px, py, deg, cat)
  titles.bin       uint32 N, uint32[N+1] offsets, utf-8 bytes
  edgeTgt.bin      uint32 N (unused padding to match viewer_full.bin's header
                   shape), then N*2 float32 (strongest-neighbor x, y; NaN pair
                   if the node has no neighbors)
  adjacency_csr.bin / adjacency_csr_rev.bin  -- see build_adjacency_csr.py
"""
import argparse
import os
import struct
import sys
import time

import numpy as np
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from build_adjacency_csr import build_csr  # noqa: E402  (reuse, don't reimplement)

CATNAMES = ['Biography', 'Science', 'History', 'Art', 'Philosophy', 'Geography', 'Other']
# Matches extract_weighted_edges.py's TOPICS keys / the live DB's category
# strings (confirmed against sampled rows: "Art & Culture", "Geography &
# Places", "Other & General", etc.) -> the same 0-6 ints engine.js's CATNAME
# array and CAT color table already index by.
CAT_STRING_TO_ID = {
    'Biography & People': 0, 'Science & Technology': 1, 'History & Society': 2,
    'Art & Culture': 3, 'Philosophy & Religion': 4, 'Geography & Places': 5,
    'Other & General': 6,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="local copy of wiki_simulation_ctxfix.db")
    ap.add_argument("--coords", required=True, help="local coordinates_rapids.bin")
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()
    t0 = time.time()

    # ---- 1. N from the live DB (authoritative row count) -------------------
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA query_only = 1")
    N = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    print(f"N (full live node count) = {N:,}", flush=True)

    with open(args.coords, "rb") as f:
        coord_n = struct.unpack("<I", f.read(4))[0]
        if coord_n != N:
            raise ValueError(f"coordinates_rapids.bin has {coord_n:,} rows, DB has {N:,} -- mismatch, aborting")
        coord_data = np.frombuffer(f.read(), dtype=np.float32).reshape(N, 6)
    px = coord_data[:, 0].copy()
    py = coord_data[:, 1].copy()
    del coord_data
    print(f"  loaded positions from {args.coords} ({time.time()-t0:.1f}s)", flush=True)

    # ---- 2. category + title, in rowid order (== node index) ---------------
    print("Reading nodes (id, title, category) ...", flush=True)
    cat = np.full(N, 6, dtype=np.uint8)  # default "Other" for any unmapped string
    titles = [None] * N
    i = 0
    for title, category in con.execute("SELECT id, category FROM nodes ORDER BY rowid"):
        titles[i] = title if title is not None else ""
        cat[i] = CAT_STRING_TO_ID.get(category, 6)
        i += 1
        if i % 1_000_000 == 0:
            print(f"  {i:,} nodes read ({time.time()-t0:.1f}s)", flush=True)
    assert i == N, f"expected {N:,} node rows, got {i:,}"
    print(f"  done ({time.time()-t0:.1f}s)", flush=True)

    # ---- 3. links: one sequential scan feeds degree + CSR + edgeTgt --------
    print("Reading links (context-filtered, same population as pathfinding) ...", flush=True)
    cur = con.execute("SELECT source_idx, target_idx FROM links WHERE context IS NOT NULL AND context != ''")
    src_chunks, tgt_chunks = [], []
    total = 0
    while True:
        rows = cur.fetchmany(5_000_000)
        if not rows:
            break
        arr = np.array(rows, dtype=np.int32)
        src_chunks.append(arr[:, 0])
        tgt_chunks.append(arr[:, 1])
        total += len(rows)
        print(f"  {total:,} rows read ({time.time()-t0:.1f}s)", flush=True)
    con.close()
    src_all = np.concatenate(src_chunks)
    tgt_all = np.concatenate(tgt_chunks)
    del src_chunks, tgt_chunks

    in_range = (src_all < N) & (tgt_all < N)
    src = src_all[in_range]
    tgt = tgt_all[in_range]
    del src_all, tgt_all, in_range
    self_loop = (src == tgt)
    src = src[~self_loop]
    tgt = tgt[~self_loop]
    print(f"  {len(src):,} in-range, non-self-loop directed edges ({time.time()-t0:.1f}s)", flush=True)

    # ---- 4. degree (both directions, matches sidebar's inDegree+outDegree) -
    print("Computing degree ...", flush=True)
    deg = np.bincount(src, minlength=N).astype(np.int64) + np.bincount(tgt, minlength=N).astype(np.int64)
    print(f"  max degree={deg.max():,} avg={deg.mean():.1f} ({time.time()-t0:.1f}s)", flush=True)

    # ---- 5. edgeTgt: each node's single highest-degree neighbor ------------
    # Purely cosmetic (ambient background hairline, never clicked, no
    # "must be a real out-link" requirement the way route hops have) -- so
    # "strongest" is just the single highest-degree neighbor overall, out or
    # in, no directional preference. Build one undirected candidate list
    # (each edge contributes both (src,tgt) and (tgt,src) as a candidate for
    # its respective owner) and take one group-argmax over it.
    #
    # The argmax must be a genuine per-owner max, not "is this candidate
    # better than what's there so far" applied as a single vectorized numpy
    # write: when an owner has 2+ candidates in the same batch, `best[dup_idx]
    # = candidate` with repeated indices just keeps whichever candidate lands
    # last in the underlying assignment, not the actual max -- confirmed wrong
    # on a 6-node fixture before this fix (a node with a degree-4 and a
    # degree-2 candidate ended up pointing at the degree-2 one). Sorting by
    # (owner, degree) and taking each owner's last row is a correct,
    # still-vectorized group-argmax.
    print("Computing strongest-neighbor (edgeTgt) ...", flush=True)
    cand_owner = np.concatenate([src, tgt])
    cand_nbr = np.concatenate([tgt, src])
    cand_deg = deg[cand_nbr]
    order = np.lexsort((cand_deg, cand_owner))  # sort by owner, then by degree ASC within each
    owner_sorted = cand_owner[order]
    nbr_sorted = cand_nbr[order]
    is_last_in_group = np.empty(len(owner_sorted), dtype=bool)
    is_last_in_group[:-1] = owner_sorted[:-1] != owner_sorted[1:]
    is_last_in_group[-1] = True  # last row of each owner-group = highest degree in that group

    best_nbr = np.full(N, -1, dtype=np.int64)
    best_nbr[owner_sorted[is_last_in_group]] = nbr_sorted[is_last_in_group]
    print(f"  done ({time.time()-t0:.1f}s)", flush=True)

    et = np.full((N, 2), np.nan, dtype=np.float32)
    has_nbr = best_nbr >= 0
    et[has_nbr, 0] = px[best_nbr[has_nbr]]
    et[has_nbr, 1] = py[best_nbr[has_nbr]]

    # ---- 6. write viewer_full.bin -------------------------------------------
    out_viewer = os.path.join(args.out_dir, "viewer_full.bin")
    print(f"Writing {out_viewer} ...", flush=True)
    raw = np.empty((N, 4), dtype=np.float32)
    raw[:, 0] = px
    raw[:, 1] = py
    raw[:, 2] = deg.astype(np.float32)
    raw[:, 3] = cat.astype(np.float32)
    with open(out_viewer, "wb") as f:
        f.write(struct.pack("<I", N))
        f.write(raw.tobytes())
    print(f"  {os.path.getsize(out_viewer)/1e6:.1f} MB", flush=True)

    # ---- 7. write titles.bin -------------------------------------------------
    out_titles = os.path.join(args.out_dir, "titles.bin")
    print(f"Writing {out_titles} ...", flush=True)
    offsets = [0]
    chunks = []
    tb = 0
    for t in titles:
        b = t.encode("utf-8")
        chunks.append(b)
        tb += len(b)
        offsets.append(tb)
    with open(out_titles, "wb") as f:
        f.write(struct.pack("<I", N))
        f.write(np.array(offsets, dtype=np.uint32).tobytes())
        f.write(b"".join(chunks))
    print(f"  {os.path.getsize(out_titles)/1e6:.1f} MB", flush=True)

    # ---- 8. write edgeTgt.bin -------------------------------------------------
    out_edge = os.path.join(args.out_dir, "edgeTgt.bin")
    print(f"Writing {out_edge} ...", flush=True)
    with open(out_edge, "wb") as f:
        f.write(struct.pack("<I", N))
        f.write(et.tobytes())
    print(f"  {os.path.getsize(out_edge)/1e6:.1f} MB", flush=True)

    # ---- 9. CSRs, via the already-verified build_csr() ------------------------
    print("Building forward (out) CSR ...", flush=True)
    build_csr(src, tgt, os.path.join(args.out_dir, "adjacency_csr.bin"), "forward/out", N)
    print("Building reverse (in) CSR ...", flush=True)
    build_csr(tgt, src, os.path.join(args.out_dir, "adjacency_csr_rev.bin"), "reverse/in", N)

    print(f"ALL DONE in {(time.time()-t0)/60:.1f}m", flush=True)


if __name__ == "__main__":
    main()
